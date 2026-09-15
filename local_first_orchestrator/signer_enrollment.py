"""Paused, authenticated legacy runtime signer enrollment.

This module deliberately has no private-key API. It coordinates one external JSON
file and the separate controller ledger through a persisted intent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

from .evidence_hash import canonical_sha256
from .operator_config import load_operator_config


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


def _raw_config(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("external operator config is missing") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("external operator config is not valid JSON") from exc
    if type(parsed) is not dict:
        raise ValueError("external operator config must be an object")
    return raw, parsed


def _with_signer_bytes(raw: bytes, public_key: str, fingerprint: str) -> bytes:
    text = raw.decode("utf-8")
    end = len(text.rstrip()) - 1
    if end < 0 or text.rstrip()[-1] != "}":
        raise ValueError("external operator config must be a JSON object")
    # Insertion is the only modification. Existing bytes, including whitespace,
    # ordering, and every value are retained exactly.
    prefix = text[:end].rstrip()
    separator = "," if prefix[-1:] != "{" else ""
    inserted = (separator + '"operator_signing_public_key":' + json.dumps(public_key, separators=(",", ":"))
                + ',"operator_signing_key_fingerprint":' + json.dumps(fingerprint, separators=(",", ":")))
    return (text[:len(prefix)] + inserted + text[len(prefix):end] + text[end:]).encode("utf-8")


def _atomic_replace(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = __import__("tempfile").mkstemp(prefix=f".{path.name}.enroll-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
    finally:
        if fd != -1: os.close(fd)
        try: os.unlink(name)
        except FileNotFoundError: pass


def _eligible(ledger: Any, ticket_ids: tuple[str, ...], repository: Path) -> dict[str, dict[str, Any]]:
    if not ticket_ids or len(set(ticket_ids)) != len(ticket_ids):
        raise ValueError("explicit ticket IDs must be non-empty and unique")
    out: dict[str, dict[str, Any]] = {}
    for ticket_id in ticket_ids:
        ticket = ledger.connection.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        binding = ledger.connection.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
        release = ledger.connection.execute("SELECT * FROM native_dependency_releases WHERE ticket_id=?", (ticket_id,)).fetchone()
        if ticket is None or binding is None or release is None:
            raise ValueError(f"ticket {ticket_id} is not eligible for signer enrollment")
        try: authority = json.loads(str(release["routing_authority_json"] or "{}"))
        except json.JSONDecodeError as exc: raise ValueError("legacy release authority is malformed") from exc
        if authority != {}: raise ValueError(f"ticket {ticket_id} is not a legacy native release")
        if binding["operator_signer_fingerprint"] is not None or binding["operator_authority_hash"] is not None:
            raise ValueError(f"ticket {ticket_id} signer binding is already set or partial")
        if str(binding["repository_path"]) != str(repository) or int(binding["ownership_verified"]) != 1 or not binding["starting_sha"] or not binding["canonical_sha"]:
            raise ValueError(f"ticket {ticket_id} runtime binding is not current and owned")
        try:
            head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repository, text=True, capture_output=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc: raise ValueError("canonical repository cannot be verified") from exc
        if head != str(binding["starting_sha"]):
            raise ValueError(f"ticket {ticket_id} starting SHA drifted")
        projection = ledger.connection.execute("""
            SELECT b.*,e.event_type,e.entity_type,e.entity_id FROM board_projection_outbox b JOIN events e ON e.id=b.event_id
            WHERE b.ticket_id=? AND b.operation='create_microticket' AND b.acknowledged_at IS NOT NULL
              AND b.superseded_at IS NULL AND b.external_task_id IS NOT NULL
        """, (ticket_id,)).fetchall()
        if (str(ticket["state"]) not in {"draft", "ready_local"} or len(projection) != 1
                or projection[0]["entity_type"] != "ticket" or projection[0]["entity_id"] != ticket_id
                or projection[0]["event_type"] not in {"generated_microticket_created", "generated_microticket_projection_recovered"}):
            raise ValueError(f"ticket {ticket_id} has ambiguous or ineligible projection")
        lifecycle = ledger.connection.execute("""
            SELECT 1 FROM attempts WHERE ticket_id=? UNION SELECT 1 FROM model_invocations WHERE ticket_id=?
            UNION SELECT 1 FROM model_stage_artifacts WHERE ticket_id=? UNION SELECT 1 FROM review_candidates WHERE ticket_id=?
            UNION SELECT 1 FROM review_results WHERE ticket_id=? UNION SELECT 1 FROM review_findings WHERE ticket_id=?
            UNION SELECT 1 FROM accepted_candidates WHERE ticket_id=? UNION SELECT 1 FROM accepted_evidence WHERE ticket_id=?
            UNION SELECT 1 FROM git_commit_intents WHERE ticket_id=? UNION SELECT 1 FROM git_commit_evidence WHERE ticket_id=?
            UNION SELECT 1 FROM hermes_execution_reconciliations WHERE ticket_id=?
            UNION SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND status='claimed'
        """, (ticket_id,) * 12).fetchone()
        if lifecycle is not None or ticket["lease_owner"] is not None:
            raise ValueError(f"ticket {ticket_id} has lifecycle evidence or an active claim")
        p = dict(projection[0])
        out[ticket_id] = {"repository_path": str(binding["repository_path"]), "starting_sha": str(binding["starting_sha"]), "canonical_sha": str(binding["canonical_sha"]), "ownership_verified": int(binding["ownership_verified"]), "projection_event_id": int(p["event_id"]), "projection_key": str(p["idempotency_key"]), "external_task_id": str(p["external_task_id"])}
    return out


def enroll_operator_signer(ledger: Any, *, config_path: Path, operator_id: str, reason: str, ticket_ids: tuple[str, ...], public_key_b64: str, fingerprint: str, failure_injector: Callable[[str], None] | None = None) -> dict[str, Any]:
    _, fingerprint, _ = _public_key(public_key_b64, fingerprint)
    config_path = Path(config_path).expanduser()
    old_raw, old_obj = _raw_config(config_path)
    existing_signed = (old_obj.get("operator_signing_public_key"), old_obj.get("operator_signing_key_fingerprint"))
    if (existing_signed[0] is None) != (existing_signed[1] is None):
        raise ValueError("operator signer fields are partially set")
    if existing_signed[0] is not None and (existing_signed != (public_key_b64, fingerprint)):
        raise ValueError("operator signer fields are already set differently")
    config = load_operator_config(config_path)
    if Path(ledger.database).resolve() != config.ledger_path.resolve():
        raise ValueError("operator config ledger identity does not match enrollment ledger")
    repository = config.canonical_repository
    authority_hash = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()
    selected = tuple(ticket_ids)
    current_hash = hashlib.sha256(old_raw).hexdigest()
    if existing_signed[0] is None:
        old_hash = current_hash
        new_raw = _with_signer_bytes(old_raw, public_key_b64, fingerprint)
        new_hash = hashlib.sha256(new_raw).hexdigest()
    else:
        old_hash = ""
        new_raw = old_raw
        new_hash = current_hash
    key_material = {"operator_id": operator_id, "reason": reason, "ticket_ids": list(selected), "fingerprint": fingerprint, "authority_hash": authority_hash, "new_config_hash": new_hash}
    key = canonical_sha256(key_material)
    now = ledger._now()
    existing_intent = ledger.connection.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
    if existing_intent is not None:
        old_hash = str(existing_intent["old_config_hash"])
        if str(existing_intent["new_config_hash"]) != new_hash:
            raise ValueError("conflicting signer enrollment replay")
    elif existing_signed[0] is not None:
        raise ValueError("signed config has no matching signer enrollment intent")
    with ledger._transaction() as conn:
        paused = conn.execute("SELECT paused FROM controller_state WHERE id=1").fetchone()
        if paused is None or not paused["paused"]: raise PermissionError("signer enrollment requires Local First paused")
        selected_bindings = json.loads(existing_intent["selected_bindings_json"]) if existing_intent is not None and existing_intent["status"] == "finalized" else _eligible(ledger, selected, repository)
        existing = conn.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO runtime_signer_enrollment_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (key, operator_id, reason, json.dumps(list(selected), separators=(",", ":")), fingerprint, authority_hash, old_hash, new_hash, json.dumps(selected_bindings, sort_keys=True, separators=(",", ":")), "pending_config", now, now))
        else:
            if tuple(json.loads(existing["ticket_ids_json"])) != selected or any(existing[x] != y for x,y in (("operator_id",operator_id),("reason",reason),("public_key_fingerprint",fingerprint),("authority_hash",authority_hash),("old_config_hash",old_hash),("new_config_hash",new_hash))):
                raise ValueError("conflicting signer enrollment replay")
            selected_bindings = json.loads(existing["selected_bindings_json"])
    if failure_injector: failure_injector("before_config_write")
    current_raw, current_obj = _raw_config(config_path)
    current_hash = hashlib.sha256(current_raw).hexdigest()
    if current_hash == old_hash:
        _atomic_replace(config_path, new_raw)
    elif current_hash != new_hash or current_obj.get("operator_signing_key_fingerprint") != fingerprint or current_obj.get("operator_signing_public_key") != public_key_b64:
        raise RuntimeError("external operator config drift or rename requires reconciliation")
    if failure_injector: failure_injector("after_config_write")
    reread, reread_obj = _raw_config(config_path)
    if hashlib.sha256(reread).hexdigest() != new_hash or reread_obj.get("operator_signing_key_fingerprint") != fingerprint or reread_obj.get("operator_signing_public_key") != public_key_b64:
        raise RuntimeError("external operator config reread proof failed")
    # Full parser proof rejects malformed/partial signer registration.
    loaded = load_operator_config(config_path)
    if loaded.operator_signing_key_fingerprint != fingerprint: raise RuntimeError("external operator config signer proof failed")
    if existing_intent is not None and existing_intent["status"] == "finalized":
        event = ledger.connection.execute("SELECT id FROM events WHERE entity_type='controller' AND event_type='runtime_signer_enrollment_completed' AND json_extract(payload_json,'$.enrollment_key')=?", (key,)).fetchone()
        if event is None: raise RuntimeError("finalized signer enrollment evidence is missing")
        return {"status":"finalized","enrollment_key":key,"ticket_ids":list(selected),"public_key_fingerprint":fingerprint,"operator_authority_hash":authority_hash,"event_id":int(event["id"])}
    # Persist that the external seam completed. The intent remains pending until
    # the binding/evidence transaction commits.
    with ledger._transaction() as conn:
        conn.execute("UPDATE runtime_signer_enrollment_intents SET status='config_written',updated_at=? WHERE enrollment_key=? AND status='pending_config'", (ledger._now(), key))
    with ledger._transaction() as conn:
        intent = conn.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE enrollment_key=?", (key,)).fetchone()
        if intent is None: raise RuntimeError("signer enrollment intent disappeared")
        if intent["new_config_hash"] != new_hash: raise ValueError("signer enrollment config identity conflicts")
        for ticket_id in selected:
            binding = conn.execute("SELECT * FROM runtime_bindings WHERE ticket_id=?", (ticket_id,)).fetchone()
            old = selected_bindings[ticket_id]
            if binding is None:
                raise RuntimeError("runtime binding drift during signer enrollment")
            actual_old = {k: binding[k] for k in ("repository_path","starting_sha","canonical_sha","ownership_verified")}
            if actual_old != {k: old[k] for k in actual_old} or binding["operator_signer_fingerprint"] is not None or binding["operator_authority_hash"] is not None:
                raise RuntimeError("runtime binding drift during signer enrollment")
        if failure_injector: failure_injector("before_binding_update")
        for ticket_id in selected:
            changed = conn.execute("UPDATE runtime_bindings SET operator_signer_fingerprint=?, operator_authority_hash=? WHERE ticket_id=? AND operator_signer_fingerprint IS NULL AND operator_authority_hash IS NULL", (fingerprint, authority_hash, ticket_id))
            if changed.rowcount != 1: raise RuntimeError("runtime binding update failed")
            old = selected_bindings[ticket_id]
            new = {**old, "operator_signer_fingerprint": fingerprint, "operator_authority_hash": authority_hash}
            evidence_hash = canonical_sha256({"enrollment_key": key, "ticket_id": ticket_id, "old": old, "new": new, "operator_id": operator_id, "reason": reason, "fingerprint": fingerprint, "authority_hash": authority_hash})
            conn.execute("INSERT INTO runtime_signer_enrollments VALUES (?,?,?,?,?,?,?,?,?,?)", (key,ticket_id,operator_id,reason,json.dumps(old,sort_keys=True,separators=(",", ":")),json.dumps(new,sort_keys=True,separators=(",", ":")),fingerprint,authority_hash,now,evidence_hash))
        new_bindings = {ticket_id: {**selected_bindings[ticket_id], "operator_signer_fingerprint": fingerprint, "operator_authority_hash": authority_hash} for ticket_id in selected}
        event_payload = {"enrollment_key":key,"old_config_hash":old_hash,"new_config_hash":new_hash,"ticket_ids":list(selected),"prior_bindings":selected_bindings,"new_bindings":new_bindings,"public_key_fingerprint":fingerprint,"operator_authority_hash":authority_hash,"operator_id":operator_id,"reason":reason}
        event_id = ledger._append_event(conn, entity_type="controller", entity_id="controller", event_type="runtime_signer_enrollment_completed", actor_id=operator_id, payload=event_payload)
        conn.execute("UPDATE runtime_signer_enrollment_intents SET status='finalized',updated_at=? WHERE enrollment_key=?", (ledger._now(),key))
        if failure_injector: failure_injector("after_finalization")
    return {"status":"finalized","enrollment_key":key,"ticket_ids":list(selected),"public_key_fingerprint":fingerprint,"operator_authority_hash":authority_hash,"event_id":event_id}
