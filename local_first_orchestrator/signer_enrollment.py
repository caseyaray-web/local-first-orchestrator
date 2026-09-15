"""External-authority enrollment for legacy runtime signer bindings.

The plugin creates a canonical request document and verifies a detached signature
made by the new key. It never accepts or handles private key material.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import secrets
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .evidence_hash import canonical_sha256
from .operator_config import load_operator_config, _parse_operator_config_bytes, _locked_config, _read_verified_fd, _config_identity, _atomic_config_replace

ENROLLMENT_DOMAIN = "local-first-operator-signer-enrollment"
ENROLLMENT_VERSION = 1
_REQUIRED_DOCUMENT_FIELDS = {
    "domain", "version", "operation", "ledger_identity", "old_config_identity", "old_config_hash",
    "new_config_hash", "new_config_bytes",
    "new_public_key", "new_fingerprint", "ticket_ids", "binding_projection_release_identities",
    "operator_id", "reason", "nonce",
}


def _public_key(value: str, fingerprint: str) -> tuple[str, str, bytes]:
    if type(value) is not str or not value or type(fingerprint) is not str or not fingerprint:
        raise ValueError("signer enrollment requires a public key and fingerprint")
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise ValueError("signer fingerprint must be lowercase SHA-256")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("signer public key must be base64") from exc
    if len(raw) != 32:
        raise ValueError("signer public key must be a raw Ed25519 public key")
    if hashlib.sha256(raw).hexdigest() != fingerprint:
        raise ValueError("signer fingerprint does not match public key")
    return value, fingerprint, raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("operator config contains duplicate JSON keys")
        result[key] = value
    return result


def _parse_config(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("external operator config is not valid JSON") from exc
    if type(value) is not dict:
        raise ValueError("external operator config must be an object")
    return value


def _raw_config(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any] | None]:
    with _locked_config(path, write=False) as (fd, identity):
        raw = _read_verified_fd(fd, identity)
    return raw, _parse_config(raw), identity


def _with_signer_bytes(raw: bytes, public_key: str, fingerprint: str) -> bytes:
    text = raw.decode("utf-8")
    stripped = text.rstrip()
    if not stripped.endswith("}"):
        raise ValueError("external operator config must be a JSON object")
    end = len(stripped) - 1
    prefix = stripped[:end].rstrip()
    separator = "," if prefix[-1:] != "{" else ""
    inserted = separator + json.dumps("operator_signing_public_key") + ":" + json.dumps(public_key, separators=(",", ":")) + "," + json.dumps("operator_signing_key_fingerprint") + ":" + json.dumps(fingerprint, separators=(",", ":"))
    return (stripped[:len(prefix)] + inserted + stripped[len(prefix):]).encode("utf-8") + text[len(stripped):].encode("utf-8")


def _canonical_document(document: dict[str, Any]) -> bytes:
    if type(document) is not dict or set(document) != _REQUIRED_DOCUMENT_FIELDS:
        raise ValueError("signer enrollment document has unexpected or missing fields")
    if document["domain"] != ENROLLMENT_DOMAIN or document["version"] != ENROLLMENT_VERSION or document["operation"] != "enroll-operator-signer":
        raise ValueError("signer enrollment document domain or version mismatch")
    for field in ("ledger_identity", "old_config_hash", "new_config_hash", "new_config_bytes", "new_fingerprint", "operator_id", "reason", "nonce", "new_public_key"):
        if type(document[field]) is not str or not document[field].strip():
            raise ValueError(f"signer enrollment document {field} is required")
    if type(document["old_config_identity"]) is not dict or type(document["ticket_ids"]) is not list or not document["ticket_ids"]:
        raise ValueError("signer enrollment document identity and tickets are required")
    if any(type(item) is not str or not item for item in document["ticket_ids"]):
        raise ValueError("signer enrollment ticket IDs must be non-empty strings")
    if type(document["binding_projection_release_identities"]) is not dict:
        raise ValueError("signer enrollment binding identities are required")
    try:
        new_raw = base64.b64decode(document["new_config_bytes"], validate=True)
    except Exception as exc:
        raise ValueError("signed new operator config bytes must be base64") from exc
    if hashlib.sha256(new_raw).hexdigest() != document["new_config_hash"]:
        raise ValueError("signed new operator config hash does not match bytes")
    _parse_config(new_raw)
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def parse_enrollment_document(document: dict[str, Any] | bytes | str) -> tuple[dict[str, Any], bytes]:
    if isinstance(document, dict):
        parsed = document
    else:
        data = document.encode() if isinstance(document, str) else document
        if not isinstance(data, bytes): raise ValueError("signer enrollment document must be JSON")
        try: parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
        except ValueError as exc:
            if "duplicate" in str(exc): raise
            raise ValueError("signer enrollment document is invalid") from exc
        if _canonical_document(parsed) != data: raise ValueError("signer enrollment document is not canonical JSON")
    return parsed, _canonical_document(parsed)


def _verify(document: dict[str, Any], signature: bytes | str, public_key_b64: str, fingerprint: str) -> tuple[bytes, bytes]:
    _, fingerprint, public_key = _public_key(public_key_b64, fingerprint)
    document, data = parse_enrollment_document(document)
    if document["new_public_key"] != public_key_b64 or document["new_fingerprint"] != fingerprint:
        raise ValueError("signed enrollment document signer identity mismatch")
    raw_sig = base64.b64decode(signature, validate=True) if isinstance(signature, str) else signature
    if not isinstance(raw_sig, bytes) or len(raw_sig) != 64:
        raise ValueError("detached signer enrollment signature must be exactly 64 bytes")
    try: Ed25519PublicKey.from_public_bytes(public_key).verify(raw_sig, data)
    except (InvalidSignature, ValueError) as exc: raise ValueError("detached signer enrollment signature verification failed") from exc
    return data, raw_sig


def _eligible(ledger: Any, ticket_ids: tuple[str, ...], repository: Path) -> dict[str, dict[str, Any]]:
    if not ticket_ids or len(set(ticket_ids)) != len(ticket_ids): raise ValueError("explicit ticket IDs must be non-empty and unique")
    out = {}
    head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repository, text=True, capture_output=True, check=True).stdout.strip()
    for ticket_id in ticket_ids:
        ticket = ledger.connection.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        binding = ledger.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        release = ledger.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket_id,)).fetchone()
        if ticket is None or binding is None or release is None: raise ValueError(f"ticket {ticket_id} is not eligible for signer enrollment")
        try: authority = json.loads(str(release["routing_authority_json"] or "{}"))
        except json.JSONDecodeError as exc: raise ValueError("legacy release authority is malformed") from exc
        if authority != {} or binding["operator_signer_fingerprint"] is not None or binding["operator_authority_hash"] is not None: raise ValueError(f"ticket {ticket_id} is not an unbound legacy release")
        if str(binding["repository_path"]) != str(repository) or int(binding["ownership_verified"]) != 1 or str(binding["starting_sha"]) != head: raise ValueError(f"ticket {ticket_id} runtime binding is not current and owned")
        projection = ledger.connection.execute("""SELECT b.*,e.event_type,e.entity_type,e.entity_id FROM board_projection_outbox b JOIN events e ON e.id=b.event_id WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL AND b.superseded_at IS NULL AND b.external_task_id IS NOT NULL""", (ticket_id,)).fetchall()
        if str(ticket["state"]) not in {"draft", "ready_local"} or len(projection) != 1 or projection[0]["entity_type"] != "ticket" or projection[0]["entity_id"] != ticket_id or projection[0]["event_type"] not in {"generated_microticket_created", "generated_microticket_projection_recovered"}: raise ValueError(f"ticket {ticket_id} has ambiguous projection")
        p = dict(projection[0])
        out[ticket_id] = {"binding": {k: binding[k] for k in ("ticket_id", "repository_path", "starting_sha", "canonical_sha", "ownership_verified")}, "projection": {"event_id": int(p["event_id"]), "idempotency_key": str(p["idempotency_key"]), "external_task_id": str(p["external_task_id"])}, "release": {k: release[k] for k in ("ticket_id", "graph_hash", "child_external_id", "parent_completion_hash", "routing_authority_json", "hermes_status")}}
    return out


def prepare_operator_signer_enrollment(ledger: Any, *, config_path: Path, operator_id: str, reason: str, ticket_ids: tuple[str, ...], public_key_b64: str, fingerprint: str, nonce: str | None = None) -> dict[str, Any]:
    public_key_b64, fingerprint, _ = _public_key(public_key_b64, fingerprint)
    if not operator_id or not reason: raise ValueError("operator identity and reason are required")
    if not nonce: nonce = secrets.token_hex(16)
    raw, obj, identity = _raw_config(Path(config_path).expanduser())
    config = load_operator_config(Path(config_path).expanduser())
    selected = _eligible(ledger, tuple(ticket_ids), config.canonical_repository)
    new_raw = _with_signer_bytes(raw, public_key_b64, fingerprint)
    document = {"domain": ENROLLMENT_DOMAIN, "version": ENROLLMENT_VERSION, "operation": "enroll-operator-signer", "ledger_identity": str(Path(ledger.database).resolve()), "old_config_identity": identity, "old_config_hash": hashlib.sha256(raw).hexdigest(), "new_config_hash": hashlib.sha256(new_raw).hexdigest(), "new_config_bytes": base64.b64encode(new_raw).decode(), "new_public_key": public_key_b64, "new_fingerprint": fingerprint, "ticket_ids": list(ticket_ids), "binding_projection_release_identities": selected, "operator_id": operator_id, "reason": reason, "nonce": nonce}
    data = _canonical_document(document)
    return {"document": document, "document_bytes": data, "document_hash": hashlib.sha256(data).hexdigest()}


def enroll_operator_signer(ledger: Any, *, config_path: Path, document: dict[str, Any] | bytes | str | None = None, detached_signature: bytes | str | None = None, public_key_b64: str | None = None, fingerprint: str | None = None, operator_id: str | None = None, reason: str | None = None, ticket_ids: tuple[str, ...] = (), failure_injector: Callable[[str], None] | None = None) -> dict[str, Any]:
    # Validate legacy key arguments before the pause check so private-key-shaped
    # input is rejected without touching config or ledger state.
    if public_key_b64 is not None and fingerprint is not None: _public_key(public_key_b64, fingerprint)
    paused = ledger.connection.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
    if paused is None or not paused["paused"]: raise PermissionError("signer enrollment requires Local First paused")
    if document is None or detached_signature is None or public_key_b64 is None or fingerprint is None:
        raise ValueError("enroll-operator-signer requires a prepared document and detached signature")
    document_obj, data = parse_enrollment_document(document)
    _verify(document_obj, detached_signature, public_key_b64, fingerprint)
    selected = tuple(document_obj["ticket_ids"])
    if tuple(ticket_ids) not in ((), selected): raise ValueError("ticket IDs conflict with signed enrollment document")
    if operator_id is not None and operator_id != document_obj["operator_id"]: raise ValueError("operator identity conflicts with signed document")
    if reason is not None and reason != document_obj["reason"]: raise ValueError("reason conflicts with signed document")
    config_path = Path(config_path).expanduser()
    key = canonical_sha256({"document_hash": hashlib.sha256(data).hexdigest(), "signature": base64.b64encode(detached_signature if isinstance(detached_signature, bytes) else base64.b64decode(detached_signature)).decode(), "ticket_ids": list(selected)})
    expected_new_raw = base64.b64decode(document_obj["new_config_bytes"], validate=True)
    expected_new_hash = hashlib.sha256(expected_new_raw).hexdigest()
    if expected_new_hash != document_obj["new_config_hash"]:
        raise RuntimeError("signed new operator config hash proof failed")
    config_lock = _locked_config(config_path, write=True)
    with config_lock as (fd, identity):
        raw = _read_verified_fd(fd, identity); obj = _parse_config(raw)
        existing = ledger.connection.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
        resume_write = False
        if existing is not None and existing["status"] == "finalized":
            if hashlib.sha256(raw).hexdigest() != expected_new_hash or raw != expected_new_raw or obj.get("operator_signing_key_fingerprint") != fingerprint:
                raise RuntimeError("finalized signer enrollment replay config mismatch")
            if existing["config_identity_json"] and json.loads(str(existing["config_identity_json"])) != identity:
                raise RuntimeError("finalized signer enrollment replay identity mismatch")
            event = ledger.connection.execute("SELECT id FROM events WHERE event_type='runtime_signer_enrollment_completed' AND json_extract(payload_json,'$.enrollment_key')=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            return {"status":"finalized","enrollment_key":key,"ticket_ids":list(selected),"public_key_fingerprint":fingerprint,"operator_authority_hash":hashlib.sha256(fingerprint.encode()).hexdigest(),"event_id":int(event["id"]) if event else None}
        if existing is not None and existing["status"] == "config_written":
            if raw != expected_new_raw or hashlib.sha256(raw).hexdigest() != expected_new_hash or obj.get("operator_signing_key_fingerprint") != fingerprint:
                raise RuntimeError("config_written signer enrollment replay config mismatch")
            stored_identity = json.loads(str(existing["config_identity_json"] or "null"))
            if stored_identity != identity:
                raise RuntimeError("config_written signer enrollment replay identity mismatch")
            resume_write = True
        elif existing is not None and existing["status"] == "pending_config" and raw == expected_new_raw and obj.get("operator_signing_key_fingerprint") == fingerprint:
            # The atomic write completed but the checkpoint did not.  This is
            # the only legal pending replay besides the exact old file.
            resume_write = True
        elif hashlib.sha256(raw).hexdigest() != document_obj["old_config_hash"] or identity != document_obj["old_config_identity"]:
            raise RuntimeError("external operator config differs from signed enrollment document")
        config = _parse_operator_config_bytes(expected_new_raw if resume_write else raw, config_path)
        if str(Path(ledger.database).resolve()) != document_obj["ledger_identity"]: raise ValueError("ledger identity conflicts with signed enrollment document")
        selected_bindings = _eligible(ledger, selected, config.canonical_repository)
        if selected_bindings != document_obj["binding_projection_release_identities"]: raise RuntimeError("ticket, projection, or release evidence drifted")
        old_hash = document_obj["old_config_hash"]
        new_raw = expected_new_raw
        new_hash = expected_new_hash
        existing = ledger.connection.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
        now = ledger._now()
        with ledger._transaction() as conn:
            if existing is None:
                conn.execute("INSERT INTO runtime_signer_enrollment_intents (enrollment_key,operator_id,reason,ticket_ids_json,public_key_fingerprint,authority_hash,old_config_hash,new_config_hash,selected_bindings_json,status,created_at,updated_at,document_json,document_hash,detached_signature,ledger_identity,old_config_identity_json,nonce,new_config_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (key,document_obj["operator_id"],document_obj["reason"],json.dumps(selected,separators=(",",":")),fingerprint,hashlib.sha256(fingerprint.encode()).hexdigest(),old_hash,new_hash,json.dumps(selected_bindings,sort_keys=True,separators=(",",":")),"pending_config",now,now,data.decode(),hashlib.sha256(data).hexdigest(),base64.b64encode(detached_signature if isinstance(detached_signature,bytes) else base64.b64decode(detached_signature)).decode(),document_obj["ledger_identity"],json.dumps(identity,sort_keys=True,separators=(",",":")),document_obj["nonce"],base64.b64encode(new_raw).decode()))
            else:
                if existing["document_hash"] != hashlib.sha256(data).hexdigest() or existing["new_config_hash"] != new_hash or existing["new_config_bytes"] not in (None, base64.b64encode(new_raw).decode()): raise ValueError("conflicting signer enrollment replay")
        if not resume_write:
            if failure_injector: failure_injector("before_config_write")
            _atomic_config_replace(config_path, new_raw, identity)
        # Atomic replace intentionally changes inode identity. Reopen the new
        # file through the already-held writer lock; never reacquire its flock.
        _, written_identity = config_lock.reopen()
        written_raw = config_lock.read()
        if hashlib.sha256(written_raw).hexdigest() != new_hash:
            raise RuntimeError("external operator config write proof failed")
        if failure_injector: failure_injector("after_config_write")
        fresh_obj = _parse_config(written_raw)
        if fresh_obj.get("operator_signing_key_fingerprint") != fingerprint:
            raise RuntimeError("external operator config reread proof failed")
        # Commit the external-effect checkpoint separately from all binding and
        # finalization writes.  A crash after this commit resumes idempotently.
        if existing is None or existing["status"] == "pending_config":
            with ledger._transaction() as conn:
                intent = conn.execute("SELECT status FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
                if intent is None or intent["status"] != "pending_config": raise RuntimeError("invalid signer enrollment state")
                conn.execute("UPDATE runtime_signer_enrollment_intents SET status='config_written',config_identity_json=?,updated_at=? WHERE enrollment_key=? AND status='pending_config'", (json.dumps(written_identity,sort_keys=True,separators=(",",":")), ledger._now(), key))
        if failure_injector: failure_injector("after_config_written")
        with ledger._transaction() as conn:
            intent = conn.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
            if intent is None or intent["status"] not in {"pending_config","config_written"}: raise RuntimeError("signer enrollment intent is not recoverable")
            for ticket_id in selected:
                binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone(); old = selected_bindings[ticket_id]["binding"]
                actual = {k: binding[k] for k in old}
                if actual != old or binding["operator_signer_fingerprint"] is not None or binding["operator_authority_hash"] is not None: raise RuntimeError("runtime binding drift during signer enrollment")
            if failure_injector: failure_injector("before_binding_update")
            for ticket_id in selected:
                if conn.execute("UPDATE runtime_bindings SET operator_signer_fingerprint=?,operator_authority_hash=? WHERE ticket_id=? AND operator_signer_fingerprint IS NULL AND operator_authority_hash IS NULL", (fingerprint,hashlib.sha256(fingerprint.encode()).hexdigest(),ticket_id)).rowcount != 1: raise RuntimeError("runtime binding update failed")
                old = selected_bindings[ticket_id]["binding"]; new = {**old,"operator_signer_fingerprint":fingerprint,"operator_authority_hash":hashlib.sha256(fingerprint.encode()).hexdigest()}
                evidence_hash = canonical_sha256({"enrollment_key":key,"ticket_id":ticket_id,"old":old,"new":new,"document_hash":hashlib.sha256(data).hexdigest()})
                conn.execute("INSERT INTO runtime_signer_enrollments (enrollment_key,ticket_id,operator_id,reason,old_binding_identity_json,new_binding_identity_json,public_key_fingerprint,authority_hash,created_at,evidence_hash,document_json,document_hash,detached_signature,config_identity_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (key,ticket_id,document_obj["operator_id"],document_obj["reason"],json.dumps(old,sort_keys=True,separators=(",",":")),json.dumps(new,sort_keys=True,separators=(",",":")),fingerprint,hashlib.sha256(fingerprint.encode()).hexdigest(),now,evidence_hash,data.decode(),hashlib.sha256(data).hexdigest(),base64.b64encode(detached_signature if isinstance(detached_signature,bytes) else base64.b64decode(detached_signature)).decode(),json.dumps(written_identity,sort_keys=True,separators=(",",":"))))
            verify_raw = config_lock.read()
            verify_obj = _parse_config(verify_raw)
            verify_identity = config_lock.stat()
            if hashlib.sha256(verify_raw).hexdigest() != new_hash or verify_obj.get("operator_signing_key_fingerprint") != fingerprint or verify_identity != written_identity:
                raise RuntimeError("external operator config changed before binding commit; reconciliation required")
            event_id = ledger._append_event(conn, entity_type="controller", entity_id="controller", event_type="runtime_signer_enrollment_completed", actor_id=document_obj["operator_id"], payload={"enrollment_key":key,"document_hash":hashlib.sha256(data).hexdigest(),"old_config_hash":old_hash,"new_config_hash":new_hash,"new_config_bytes":base64.b64encode(new_raw).decode("ascii"),"config_identity":written_identity,"ticket_ids":list(selected),"public_key_fingerprint":fingerprint,"operator_id":document_obj["operator_id"],"reason":document_obj["reason"]})
            conn.execute("UPDATE runtime_signer_enrollment_intents SET status='finalized',updated_at=?,config_identity_json=? WHERE enrollment_key=? AND status='config_written'", (ledger._now(),json.dumps(written_identity,sort_keys=True,separators=(",",":")),key))
        # This seam is deliberately after COMMIT. A post-commit failure must
        # be durably quarantined, not rolled back into an apparently replayable
        # partial enrollment.
        try:
            if failure_injector: failure_injector("after_finalization")
            post_raw = config_lock.read()
            post_obj = _parse_config(post_raw)
            post_identity = config_lock.stat()
            if hashlib.sha256(post_raw).hexdigest() != new_hash or post_obj.get("operator_signing_key_fingerprint") != fingerprint or post_identity != written_identity:
                raise RuntimeError("external operator config post-commit proof failed")
        except Exception as exc:
            with ledger._transaction() as conn:
                conn.execute("UPDATE runtime_signer_enrollment_intents SET status='invalidated',updated_at=? WHERE enrollment_key=? AND status='finalized'", (ledger._now(), key))
                ledger._append_event(conn, entity_type="controller", entity_id="controller", event_type="runtime_signer_enrollment_invalidated", actor_id=document_obj["operator_id"], payload={"enrollment_key":key,"reason":str(exc)})
                conn.execute("UPDATE controller_state SET paused=1,updated_at=? WHERE id=1", (ledger._now(),))
            raise
        return {"status":"finalized","enrollment_key":key,"ticket_ids":list(selected),"public_key_fingerprint":fingerprint,"operator_authority_hash":hashlib.sha256(fingerprint.encode()).hexdigest(),"event_id":event_id}
