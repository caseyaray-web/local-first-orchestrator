from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.operator_config import enroll_operator_signer, load_operator_config
from local_first_orchestrator.signer_enrollment import (
    ENROLLMENT_DOMAIN, ENROLLMENT_VERSION, parse_enrollment_document,
    _canonical_document, _verify, prepare_operator_signer_enrollment,
)
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState


class OperatorSignerEnrollmentRedTests(unittest.TestCase):
    def _enrollment_fixture(self):
        tmp = TemporaryDirectory(); root = Path(tmp.name); repo = root / "repo"; repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
        (repo / "README").write_text("fixture\n")
        subprocess.run(("git", "add", "README"), cwd=repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=repo, check=True)
        head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        ledger = Ledger(root / "ledger.db"); ledger.migrate()
        ticket = ledger.create_ticket(title="legacy", state=CanonicalState.READY_LOCAL, external_id="external-legacy", contract={"objective":"x","criterion_ids":["AC-1"],"primary_symbol":"x","allowed_files":["README"],"forbidden_changes":[],"patch_budget":{"max_files":1,"max_changed_lines":1},"verification":{"commands":[]},"risk":"low","review_required":True,"max_attempts":1,"dependencies":[]})
        ledger.bind_runtime(ticket, str(repo), head)
        with ledger._transaction() as conn:
            event = ledger._append_event(conn, entity_type="ticket", entity_id=ticket, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id":ticket})
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,acknowledged_at,external_task_id,operation) VALUES (?,?,?,'{}',?,1,1,?,'create_microticket')", (ticket,event,"draft","legacy-create","external-legacy"))
            conn.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,1)", (ticket,"external-legacy","[]","[]","graph-hash"))
            conn.execute("INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,1)", (ticket,"graph-hash","external-legacy","parent","{}","ready"))
        config_path = root / "operator.json"
        config_path.write_text(json.dumps({"ledger_path":str(root / "ledger.db"),"canonical_repository":str(repo),"repository_allowlist":[str(repo)],"implementation":{"profile":"i","provider":"p","model":"m"},"review":{"profile":"r","provider":"p","model":"m"},"worktree_root":str(root / "w"),"artifact_root":str(root / "a"),"implementation_timeout_seconds":30,"review_timeout_seconds":30}, indent=2) + "\n")
        ledger.pause("operator", reason="maintenance")
        private = Ed25519PrivateKey.generate(); public = private.public_key().public_bytes_raw(); public_b64 = base64.b64encode(public).decode(); fingerprint = hashlib.sha256(public).hexdigest()
        prepared = prepare_operator_signer_enrollment(ledger, config_path=config_path, operator_id="operator", reason="migration", ticket_ids=(ticket,), public_key_b64=public_b64, fingerprint=fingerprint, nonce="fixed")
        signature = private.sign(prepared["document_bytes"])
        return tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket

    def test_enrollment_document_is_canonical_duplicate_free_and_detached_signed(self) -> None:
        private = Ed25519PrivateKey.generate(); public = private.public_key().public_bytes_raw()
        public_b64 = base64.b64encode(public).decode(); fingerprint = hashlib.sha256(public).hexdigest()
        document = {
            "domain": ENROLLMENT_DOMAIN, "version": ENROLLMENT_VERSION, "operation": "enroll-operator-signer",
            "ledger_identity": "/tmp/fixture-ledger.db", "old_config_identity": {"dev": 1, "ino": 2, "uid": os.getuid(), "mode": 0o600, "path": "/tmp/operator.json"},
            "old_config_hash": "a" * 64, "new_config_hash": "3f024a3d64024208cb2b7b024edc1cdd90c90002a97157c233b02ef773fd68d7", "new_config_bytes": base64.b64encode(b'{"signed":true}').decode(), "new_public_key": public_b64, "new_fingerprint": fingerprint,
            "ticket_ids": ["ticket-1"], "binding_projection_release_identities": {"ticket-1": {"binding": {}, "projection": {}, "release": {}}},
            "operator_id": "operator", "reason": "legacy migration", "nonce": "nonce-1",
        }
        encoded = _canonical_document(document)
        signature = private.sign(encoded)
        self.assertEqual(parse_enrollment_document(encoded)[0], document)
        self.assertEqual(_verify(document, signature, public_b64, fingerprint)[0], encoded)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_enrollment_document(encoded.replace(b'"nonce":"nonce-1"', b'"nonce":"nonce-1","nonce":"nonce-2"'))

    def test_config_duplicate_null_and_symlink_inputs_stop_before_registration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); target = root / "target.json"; link = root / "operator.json"
            target.write_text('{"ledger_path":"x","ledger_path":"y"}')
            link.symlink_to(target)
            with self.assertRaises((ValueError, OSError)):
                load_operator_config(link)
            target.unlink(); link.unlink()
            target.write_text('{"ledger_path":null}')
            with self.assertRaises(ValueError):
                load_operator_config(target)

    def test_enrollment_api_is_paused_and_never_accepts_private_key(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"; repo.mkdir()
            subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
            (repo / "README").write_text("fixture\n")
            subprocess.run(("git", "add", "README"), cwd=repo, check=True)
            subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=repo, check=True)
            ledger_path = root / "ledger.db"; ledger = Ledger(ledger_path); ledger.migrate()
            config_path = root / "operator.json"
            config_path.write_bytes((json.dumps({
                "ledger_path": str(ledger_path), "canonical_repository": str(repo),
                "repository_allowlist": [str(repo)],
                "implementation": {"profile": "impl", "provider": "p", "model": "m"},
                "review": {"profile": "review", "provider": "p", "model": "m"},
                "worktree_root": str(root / "worktrees"), "artifact_root": str(root / "artifacts"),
                "implementation_timeout_seconds": 300, "review_timeout_seconds": 300,
            }, indent=2) + "\n").encode())
            key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
            fingerprint = hashlib.sha256(key).hexdigest()
            with self.assertRaisesRegex(PermissionError, "paused"):
                enroll_operator_signer(ledger, config_path=config_path, operator_id="op", reason="legacy", ticket_ids=(), public_key_b64=base64.b64encode(key).decode(), fingerprint=fingerprint)
            with self.assertRaises((ValueError, TypeError)):
                enroll_operator_signer(ledger, config_path=config_path, operator_id="op", reason="legacy", ticket_ids=(), public_key_b64="private-key", fingerprint=fingerprint)
            self.assertNotIn(key.hex(), config_path.read_text())
            ledger.close()

    def test_after_config_write_replays_exact_new_bytes_and_rejects_conflict(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        def crash(seam):
            if seam == "after_config_write": raise RuntimeError("injected")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint, failure_injector=crash)
        intent = ledger.connection.execute("SELECT status FROM runtime_signer_enrollment_intents").fetchone()
        self.assertEqual(intent["status"], "pending_config")
        self.assertEqual(preview_next(ledger).next_stage, "reconciliation_required")
        result = enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        self.assertEqual(result["status"], "finalized")
        self.assertEqual(ledger.connection.execute("SELECT status FROM runtime_signer_enrollment_intents").fetchone()["status"], "finalized")
        self.assertEqual(ledger.connection.execute("SELECT operator_signer_fingerprint FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()[0], fingerprint)
        ledger.close(); tmp.cleanup()

    def test_config_written_checkpoint_crash_replays_bindings_and_finalization(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        def crash(seam):
            if seam == "after_config_written": raise RuntimeError("checkpoint crash")
        with self.assertRaisesRegex(RuntimeError, "checkpoint crash"):
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint, failure_injector=crash)
        self.assertEqual(ledger.connection.execute("SELECT status FROM runtime_signer_enrollment_intents").fetchone()[0], "config_written")
        self.assertEqual(preview_next(ledger).reconciliation_action, "stop")
        self.assertEqual(enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)["status"], "finalized")
        self.assertEqual(ledger.connection.execute("SELECT COUNT(*) FROM runtime_signer_enrollments").fetchone()[0], 1)
        ledger.close(); tmp.cleanup()

    def test_pending_replay_rejects_exactly_any_third_config_state(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        def crash(seam):
            if seam == "after_config_write": raise RuntimeError("injected")
        with self.assertRaises(RuntimeError):
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint, failure_injector=crash)
        config_path.write_bytes(base64.b64decode(prepared["document"]["new_config_bytes"]) + b" ")
        with self.assertRaisesRegex(RuntimeError, "differs from signed enrollment document"):
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        self.assertEqual(ledger.connection.execute("SELECT status FROM runtime_signer_enrollment_intents").fetchone()[0], "pending_config")
        ledger.close(); tmp.cleanup()

    def test_binding_phase_crash_leaves_config_written_checkpoint_for_replay(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        def crash(seam):
            if seam == "before_binding_update": raise RuntimeError("binding crash")
        with self.assertRaisesRegex(RuntimeError, "binding crash"):
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint, failure_injector=crash)
        self.assertEqual(ledger.connection.execute("SELECT status FROM runtime_signer_enrollment_intents").fetchone()[0], "config_written")
        self.assertEqual(enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)["status"], "finalized")
        ledger.close(); tmp.cleanup()

    def test_detector_rejects_forged_finalized_enrollment_for_same_signer_altered_bytes(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        altered = config_path.read_bytes() + b" "
        config_path.write_bytes(altered)
        altered_hash = hashlib.sha256(altered).hexdigest()
        enrollment_key = ledger.connection.execute("SELECT enrollment_key FROM runtime_signer_enrollment_intents").fetchone()[0]
        from local_first_orchestrator.operator_config import _config_identity
        altered_identity = _config_identity(config_path)
        ledger.connection.executescript("""
            DROP TRIGGER events_immutable_update;
            DROP TRIGGER runtime_signer_enrollment_intents_immutable_identity;
            DROP TRIGGER runtime_signer_enrollments_immutable_update;
        """)
        ledger.connection.execute("UPDATE runtime_signer_enrollment_intents SET new_config_hash=?,config_identity_json=? WHERE enrollment_key=?", (altered_hash, json.dumps(altered_identity, sort_keys=True, separators=(",", ":")), enrollment_key))
        ledger.connection.execute("UPDATE runtime_signer_enrollments SET config_identity_json=? WHERE enrollment_key=?", (json.dumps(altered_identity, sort_keys=True, separators=(",", ":")), enrollment_key))
        event = ledger.connection.execute("SELECT id,payload_json FROM events WHERE event_type='runtime_signer_enrollment_completed'").fetchone()
        payload = json.loads(event["payload_json"]); payload["new_config_hash"] = altered_hash
        ledger.connection.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), event["id"]))
        blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
        self.assertIsNotNone(blocker)
        self.assertIn("signer enrollment reconciliation required", blocker["reason"])
        self.assertIn("signed new config", blocker["reason"])
        ledger.connection.execute("UPDATE controller_state SET paused=0 WHERE id=1")
        preview = preview_next(ledger, signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, signer_config_path=config_path)
        self.assertEqual(preview.next_stage, "reconciliation_required")
        self.assertTrue((preview.blocker_reason or "").startswith("signer enrollment reconciliation required:"))
        class NoSideEffectsBoard:
            timeout_seconds = 1
        with self.assertRaisesRegex(RuntimeError, "signer enrollment reconciliation required"):
            ProcessNextScheduler(ledger, NoSideEffectsBoard(), worker_id="fixture", lease_seconds=30, clock=lambda: 100, native_dependency_release_signer_public_key=base64.b64decode(public_b64), native_dependency_release_signer_fingerprint=fingerprint, native_dependency_release_signer_config_path=config_path).process_next()
        ledger.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
