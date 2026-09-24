"""C12R1-TK-3-only, root-custodied interactive approval boundary.

This module is intentionally not a production installer.  It is the reviewed
helper payload which must be copied to a root-owned immutable snapshot before
it is ever invoked through sudo.  It only prepares/signs documents and invokes
the two already-supported ledger CLI paths as the configured SQLite owner.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

TARGET_TICKET = "C12R1-TK-3"
TARGET_ATTEMPT = 2
ACTOR = "ocadmin"
ENROLLMENT_REASON = "C12R1-TK-3 root-custodied human approval enrollment"
RECOVERY_REASON = "C12R1-TK-3 root-custodied human approval stale-routing recovery"


@dataclass(frozen=True)
class SourcePrerequisite:
    source_root: Path
    python_executable: Path


@dataclass(frozen=True)
class HelperRuntime:
    ledger_path: Path
    config_path: Path
    installed_source: SourcePrerequisite
    key_path: Path
    actor: str = ACTOR
    exchange_parent: Path | None = None


@dataclass(frozen=True)
class PreparedApproval:
    ticket_id: str
    document_path: Path
    document_hash: str
    config_hash: str
    public_key_b64: str
    fingerprint: str
    request_id: str | None = None


Runner = Callable[[Sequence[str]], str]
Identity = Callable[[], tuple[int, bool, bool, int, int]]


def _protected_chain(path: Path) -> None:
    """Check every existing path component, not just the final inode."""
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError("protected path must be absolute and normalized")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        item = os.lstat(current)
        if stat.S_ISLNK(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
            raise PermissionError("protected path chain is not root-owned and immutable")


def validate_root_owned_source(prerequisite: SourcePrerequisite) -> None:
    """Require root-owned source, interpreter and their complete path chains."""
    root = prerequisite.source_root
    _protected_chain(root)
    _protected_chain(prerequisite.python_executable)
    if not root.is_dir() or not stat.S_ISREG(os.lstat(prerequisite.python_executable).st_mode):
        raise PermissionError("installed source/interpreter has wrong type")
    if not os.lstat(prerequisite.python_executable).st_mode & 0o111:
        raise PermissionError("installed helper Python is not executable")
    found = False
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            item = Path(current) / name
            info = os.lstat(item)
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise PermissionError("installed source contains mutable or symlinked paths")
            if name.endswith(".py"):
                found = True
                if not stat.S_ISREG(info.st_mode):
                    raise PermissionError("installed Python source must be regular")
    if not found:
        raise PermissionError("installed helper source is empty")


def _live_identity() -> tuple[int, bool, bool, int, int]:
    return os.geteuid(), sys.stdin.isatty(), sys.stdout.isatty(), int(os.environ.get("SUDO_UID", "-1")), int(os.environ.get("SUDO_GID", "-1"))


def _confirm_exact(message: str) -> bool:
    print(message, flush=True)
    expected = "APPROVE " + message.rsplit(" ", 1)[-1]
    return input(f"Type exactly '{expected}' to continue: ") == expected


class KeylessHumanRecovery:
    """Fail-closed coordinator; it cannot dispatch, resume, or touch a board."""
    def __init__(self, runtime: HelperRuntime, *, runner: Callable[..., str] | None = None,
                 confirmer: Callable[[str], bool] = _confirm_exact,
                 identity: Identity = _live_identity,
                 source_validator: Callable[[SourcePrerequisite], None] = validate_root_owned_source) -> None:
        self.runtime = runtime
        self._runner = runner or self._run_as_actor
        self._confirmer = confirmer
        self._identity = identity
        self._source_validator = source_validator

    def _guard(self) -> tuple[int, int]:
        euid, stdin_tty, stdout_tty, sudo_uid, sudo_gid = self._identity()
        if euid != 0 or not stdin_tty or not stdout_tty:
            raise PermissionError("helper requires an interactive root terminal")
        if sudo_uid < 0 or sudo_gid < 0:
            raise PermissionError("helper must be invoked through sudo, not direct root")
        try:
            sudo_name = pwd.getpwuid(sudo_uid).pw_name
        except KeyError as exc:
            raise PermissionError("sudo invoker account is unavailable") from exc
        if sudo_name != ACTOR:
            raise PermissionError("helper sudo invoker is not ocadmin")
        if self.runtime.actor != ACTOR:
            raise PermissionError("helper actor is fixed to ocadmin")
        self._source_validator(self.runtime.installed_source)
        return sudo_uid, sudo_gid

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _key(self) -> tuple[Ed25519PrivateKey, str, str]:
        path = self.runtime.key_path
        if os.geteuid() == 0:
            _protected_chain(path.parent)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.geteuid() == 0 and stat.S_IMODE(os.lstat(path.parent).st_mode) != 0o700:
            raise PermissionError("root key directory must be mode 0700")
        try:
            item = os.lstat(path)
            mode = stat.S_IMODE(item.st_mode)
            if stat.S_ISLNK(item.st_mode) or mode != 0o600 or (os.geteuid() == 0 and item.st_uid != 0):
                raise PermissionError("root signing key must be root-owned mode 0600")
            data = path.read_bytes()
            key = Ed25519PrivateKey.from_private_bytes(data)
        except FileNotFoundError:
            key = Ed25519PrivateKey.generate()
            raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, raw); os.fsync(fd)
            finally:
                os.close(fd)
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return key, base64.b64encode(public).decode("ascii"), hashlib.sha256(public).hexdigest()

    def _exchange(self, uid: int, gid: int) -> Path:
        # Keep prepared evidence across a crash.  It is safe to retry only with
        # the same bytes and a new interactive confirmation; do not silently
        # delete the evidence before the supported ledger path has checkpointed.
        parent = self.runtime.exchange_parent
        if parent is None or parent == self.runtime.key_path.parent:
            raise PermissionError("exchange parent must be distinct from the private-key directory")
        if os.geteuid() == 0:
            _protected_chain(parent)
            if stat.S_IMODE(os.lstat(parent).st_mode) & 0o005 != 0o005:
                raise PermissionError("exchange parent must be traversable by the ledger owner")
        directory = Path(tempfile.mkdtemp(dir=parent, prefix="c12r1-tk-3-"))
        # The CLI gets only this incoming directory.  Root promotes its output
        # into the parent before displaying or signing it.
        os.chmod(directory, 0o711)
        incoming = directory / "incoming"
        incoming.mkdir(mode=0o700)
        os.chown(incoming, uid, gid); os.chmod(incoming, 0o700)
        return directory

    @staticmethod
    def _read_regular_nofollow(path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PermissionError("approval evidence must be a regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    @staticmethod
    def _write_new_nofollow(path: Path, data: bytes, mode: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), mode)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        # Read the named output again with O_NOFOLLOW.  This proves both the
        # strict detached-signature bytes and that no replacement raced us.
        if KeylessHumanRecovery._read_regular_nofollow(path) != data:
            raise RuntimeError("protected approval output readback mismatch")

    def _promote_document(self, exchange: Path, incoming: Path, name: str) -> tuple[Path, bytes]:
        data = self._read_regular_nofollow(incoming)
        document = exchange / name
        self._write_new_nofollow(document, data, 0o644)
        if os.geteuid() == 0:
            os.chown(document, 0, 0)
        return document, data

    def _sign_exact_document(self, prepared: PreparedApproval, key: Ed25519PrivateKey, signature_name: str) -> Path:
        data = self._read_regular_nofollow(prepared.document_path)
        if hashlib.sha256(data).hexdigest() != prepared.document_hash:
            raise RuntimeError("approval evidence drifted after confirmation")
        signature = prepared.document_path.with_name(signature_name)
        signed = key.sign(data)
        self._write_new_nofollow(signature, signed, 0o644)
        if os.geteuid() == 0:
            os.chown(signature, 0, 0)
        public = base64.b64decode(prepared.public_key_b64, validate=True)
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(self._read_regular_nofollow(signature), data)
        except Exception as exc:
            raise RuntimeError("detached signature readback proof failed") from exc
        return signature

    def _argv(self, *command: str) -> list[str]:
        # -I rejects user Python path/site customizations. The module must be
        # available only from the reviewed, installed root-owned snapshot.
        return [str(self.runtime.installed_source.python_executable), "-I", "-c", "import sys;sys.path.insert(0," + repr(str(self.runtime.installed_source.source_root)) + ");from local_first_orchestrator.cli import main;raise SystemExit(main())", "--database", str(self.runtime.ledger_path), "--operator-config-path", str(self.runtime.config_path), *command]

    def _run_as_actor(self, argv: Sequence[str], *, uid: int, gid: int) -> str:
        def drop_privileges() -> None:
            os.setgroups([]); os.setgid(gid); os.setuid(uid)
        completed = subprocess.run(tuple(argv), text=True, capture_output=True, check=False, preexec_fn=drop_privileges, env={"PATH": "/usr/bin:/bin", "HOME": f"/home/{self.runtime.actor}", "LANG": "C.UTF-8"})
        if completed.returncode:
            raise RuntimeError(f"supported ledger command failed: {completed.stderr.strip()}")
        return completed.stdout

    def _call(self, argv: list[str], uid: int, gid: int) -> str:
        # No shell, sudo, board option, dispatch command, or caller-controlled command.
        return self._runner(argv, uid=uid, gid=gid)

    def prepare_enrollment(self, *, ticket_id: str = TARGET_TICKET) -> PreparedApproval:
        uid, gid = self._guard()
        if ticket_id != TARGET_TICKET:
            raise ValueError("helper is permanently scoped to C12R1-TK-3")
        _, public_key, fingerprint = self._key()
        exchange = self._exchange(uid, gid)
        incoming = exchange / "incoming" / "enrollment.json"
        self._call(self._argv("prepare-operator-signer-enrollment", "--operator-id", self.runtime.actor, "--reason", ENROLLMENT_REASON, "--ticket-id", TARGET_TICKET, "--public-key", public_key, "--fingerprint", fingerprint, "--output-file", str(incoming)), uid, gid)
        document, data = self._promote_document(exchange, incoming, "enrollment.json")
        return PreparedApproval(TARGET_TICKET, document, hashlib.sha256(data).hexdigest(), self._hash(self.runtime.config_path), public_key, fingerprint)

    def confirm_and_enroll(self, prepared: PreparedApproval) -> None:
        uid, gid = self._guard()
        if prepared.ticket_id != TARGET_TICKET or prepared.config_hash != self._hash(self.runtime.config_path) or prepared.document_hash != self._hash(prepared.document_path):
            raise RuntimeError("enrollment evidence/config drifted; prepare again")
        if not self._confirmer(f"ENROLL C12R1-TK-3 exact document SHA-256 {prepared.document_hash}"):
            raise PermissionError("human declined enrollment")
        key, _, _ = self._key()
        signature = self._sign_exact_document(prepared, key, "enrollment.sig")
        self._call(self._argv("enroll-operator-signer", "--operator-id", self.runtime.actor, "--reason", ENROLLMENT_REASON, "--ticket-id", TARGET_TICKET, "--public-key", prepared.public_key_b64, "--fingerprint", prepared.fingerprint, "--document-file", str(prepared.document_path), "--signature-file", str(signature)), uid, gid)

    def prepare_stale_routing_recovery(self) -> PreparedApproval:
        uid, gid = self._guard()
        exchange = self._exchange(uid, gid)
        incoming = exchange / "incoming" / "recovery.json"; request_id = uuid.uuid4().hex
        self._call(self._argv("prepare-stale-routing-recovery", "--task-id", TARGET_TICKET, "--attempt-number", str(TARGET_ATTEMPT), "--operator-id", self.runtime.actor, "--reason", RECOVERY_REASON, "--request-id", request_id, "--output-file", str(incoming)), uid, gid)
        document, data = self._promote_document(exchange, incoming, "recovery.json")
        _, public_key, fingerprint = self._key()
        return PreparedApproval(TARGET_TICKET, document, hashlib.sha256(data).hexdigest(), self._hash(self.runtime.config_path), public_key, fingerprint, request_id)

    def confirm_and_recover_stale_routing(self, prepared: PreparedApproval) -> None:
        uid, gid = self._guard()
        if prepared.ticket_id != TARGET_TICKET or not prepared.request_id or prepared.config_hash != self._hash(self.runtime.config_path) or prepared.document_hash != self._hash(prepared.document_path):
            raise RuntimeError("recovery evidence/config drifted; prepare again")
        if not self._confirmer(f"RECOVER C12R1-TK-3 attempt 2 exact document SHA-256 {prepared.document_hash}"):
            raise PermissionError("human declined stale-routing recovery")
        key, _, _ = self._key(); signature = self._sign_exact_document(prepared, key, "recovery.sig")
        self._call(self._argv("recover-stale-routing", "--task-id", TARGET_TICKET, "--attempt-number", str(TARGET_ATTEMPT), "--operator-id", self.runtime.actor, "--reason", RECOVERY_REASON, "--request-id", prepared.request_id, "--approval-file", str(prepared.document_path), "--signature-file", str(signature)), uid, gid)

    def _reconciliation(self) -> tuple[str, sqlite3.Row | None]:
        """Classify the one fixed enrollment without inventing new authority."""
        config_raw = self._read_regular_nofollow(self.runtime.config_path)
        try:
            config = json.loads(config_raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("configured signer authority is malformed") from exc
        fingerprint = config.get("operator_signing_key_fingerprint") if type(config) is dict else None
        public_key = config.get("operator_signing_public_key") if type(config) is dict else None
        if fingerprint is None and public_key is None:
            fingerprint = public_key = ""
        elif type(fingerprint) is not str or type(public_key) is not str:
            raise RuntimeError("configured signer authority is malformed")
        with sqlite3.connect(f"file:{self.runtime.ledger_path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM runtime_signer_enrollment_intents WHERE operator_id=? AND reason=?", (self.runtime.actor, ENROLLMENT_REASON)).fetchall()
            if len(rows) > 1:
                raise RuntimeError("ambiguous signer enrollment reconciliation")
            binding = db.execute("SELECT operator_signer_fingerprint,operator_authority_hash FROM runtime_bindings WHERE ticket_id=?", (TARGET_TICKET,)).fetchone()
            archive = db.execute("SELECT document_json,detached_signature,operator_id,reason FROM stale_routing_recovery_archives WHERE ticket_id=? AND attempt_number=?", (TARGET_TICKET, TARGET_ATTEMPT)).fetchall()
        if len(archive) > 1:
            raise RuntimeError("ambiguous stale-routing recovery reconciliation")
        if archive:
            item = archive[0]
            try:
                document = json.loads(item["document_json"])
                authority = document["authority"]
                exact_recovery = (item["operator_id"] == self.runtime.actor and item["reason"] == RECOVERY_REASON and authority["ticket_id"] == TARGET_TICKET and authority["attempt_number"] == TARGET_ATTEMPT and authority["signer_fingerprint"] == fingerprint)
            except (KeyError, TypeError, json.JSONDecodeError):
                exact_recovery = False
            if not exact_recovery:
                raise RuntimeError("stale-routing recovery evidence drifted")
            return "recovered", None
        if not rows:
            if binding is not None and binding["operator_signer_fingerprint"] is not None:
                raise RuntimeError("signer binding exists without exact enrollment evidence")
            return "fresh", None
        intent = rows[0]
        try:
            selected = json.loads(intent["ticket_ids_json"])
            document = json.loads(intent["document_json"])
            expected_fingerprint = document["new_fingerprint"]
            expected_public_key = document["new_public_key"]
            old_config_pending = (
                intent["status"] == "pending_config"
                and not fingerprint and not public_key
                and hashlib.sha256(config_raw).hexdigest() == document["old_config_hash"] == intent["old_config_hash"]
                and binding is not None and binding["operator_signer_fingerprint"] is None
                and binding["operator_authority_hash"] is None
            )
            new_config_present = fingerprint == expected_fingerprint and public_key == expected_public_key
            exact = (selected == [TARGET_TICKET] and intent["public_key_fingerprint"] == expected_fingerprint
                and (old_config_pending or new_config_present)
                and document["operator_id"] == self.runtime.actor and document["reason"] == ENROLLMENT_REASON)
        except (KeyError, TypeError, json.JSONDecodeError):
            exact = False
        if not exact or binding is None:
            raise RuntimeError("signer enrollment evidence drifted")
        if intent["status"] == "finalized":
            with sqlite3.connect(f"file:{self.runtime.ledger_path}?mode=ro", uri=True) as db:
                events = db.execute("SELECT payload_json FROM events WHERE event_type='runtime_signer_enrollment_completed' AND json_extract(payload_json,'$.enrollment_key')=?", (intent["enrollment_key"],)).fetchall()
            if binding["operator_signer_fingerprint"] != fingerprint or len(events) != 1:
                raise RuntimeError("finalized signer binding drifted")
            try:
                event = json.loads(events[0][0])
                if event["public_key_fingerprint"] != fingerprint or event["ticket_ids"] != [TARGET_TICKET] or event["operator_id"] != self.runtime.actor or event["reason"] != ENROLLMENT_REASON:
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("finalized signer event drifted") from exc
            return "enrolled", intent
        if intent["status"] not in {"pending_config", "config_written"}:
            raise RuntimeError("signer enrollment is not safely recoverable")
        return "partial", intent

    def _resume_exact_enrollment(self, intent: sqlite3.Row) -> None:
        """Replay only the already signed, durably recorded enrollment bytes."""
        uid, gid = self._guard()
        document = str(intent["document_json"]).encode("utf-8")
        signature = base64.b64decode(str(intent["detached_signature"]), validate=True)
        exchange = self._exchange(uid, gid)
        document_path = exchange / "enrollment.json"
        signature_path = exchange / "enrollment.sig"
        self._write_new_nofollow(document_path, document, 0o644)
        self._write_new_nofollow(signature_path, signature, 0o644)
        self._call(self._argv("enroll-operator-signer", "--operator-id", self.runtime.actor, "--reason", ENROLLMENT_REASON, "--ticket-id", TARGET_TICKET, "--public-key", json.loads(document)["new_public_key"], "--fingerprint", str(intent["public_key_fingerprint"]), "--document-file", str(document_path), "--signature-file", str(signature_path)), uid, gid)

    def run(self) -> None:
        state, intent = self._reconciliation()
        if state == "recovered":
            return
        if state == "partial":
            assert intent is not None
            self._resume_exact_enrollment(intent)
            state, intent = self._reconciliation()
            if state != "enrolled":
                raise RuntimeError("signer enrollment replay did not finalize exactly")
        elif state == "fresh":
            enrollment = self.prepare_enrollment()
            self.confirm_and_enroll(enrollment)
            state, intent = self._reconciliation()
            if state != "enrolled":
                raise RuntimeError("signer enrollment did not finalize exactly")
        recovery = self.prepare_stale_routing_recovery()
        self.confirm_and_recover_stale_routing(recovery)

ROOT_BUNDLE = Path("/usr/local/lib/local-first-orchestrator/c12r1-tk-3")


def _parse_installed_runtime(raw: object, *, bundle_root: Path = ROOT_BUNDLE) -> HelperRuntime:
    required = {"ledger_path", "config_path", "source_root", "python_executable", "key_path", "exchange_parent"}
    if (not isinstance(raw, dict) or set(raw) != required or
            any(not isinstance(raw[name], str) or not raw[name].startswith("/") or ".." in Path(raw[name]).parts
                for name in required)):
        raise ValueError("installed helper manifest is malformed")
    source = bundle_root / "source"
    python = Path("/usr/bin/python3").resolve()
    if raw["source_root"] != str(source) or raw["python_executable"] != str(python):
        raise PermissionError("installed helper manifest redirects protected code or interpreter")
    return HelperRuntime(Path(raw["ledger_path"]), Path(raw["config_path"]),
                         SourcePrerequisite(source, python), Path(raw["key_path"]),
                         ACTOR, Path(raw["exchange_parent"]))


def _load_installed_runtime(path: Path = ROOT_BUNDLE / "runtime.json") -> HelperRuntime:
    """Load only the root-owned manifest inside the atomically published bundle."""
    import json
    if path != ROOT_BUNDLE / "runtime.json":
        raise PermissionError("runtime manifest must be in the protected bundle")
    _protected_chain(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise PermissionError("installed helper manifest must be root-owned and non-writable")
    return _parse_installed_runtime(json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    """Installed root entry point; intentionally has no path/ticket arguments."""
    helper = KeylessHumanRecovery(_load_installed_runtime())
    helper.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
