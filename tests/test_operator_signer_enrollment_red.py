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
    _canonical_document, _verify, _eligible, prepare_operator_signer_enrollment,
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
        commit_a = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        (repo / "README").write_text("advanced\n")
        subprocess.run(("git", "add", "README"), cwd=repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "advanced"), cwd=repo, check=True)
        commit_b = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
        (repo / "dirty.txt").write_text("canonical remains dirty\n")
        ledger = Ledger(root / "ledger.db"); ledger.migrate()
        ticket = ledger.create_ticket(title="legacy", state=CanonicalState.READY_LOCAL, external_id="external-legacy", contract={"objective":"x","criterion_ids":["AC-1"],"primary_symbol":"x","allowed_files":["README"],"forbidden_changes":[],"patch_budget":{"max_files":1,"max_changed_lines":1},"verification":{"commands":[]},"risk":"low","review_required":True,"max_attempts":1,"dependencies":[]})
        ledger.bind_runtime(ticket, str(repo), commit_a)
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

    def test_historical_binding_enrolls_without_touching_dirty_canonical_checkout(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        repo = root / "repo"
        before = subprocess.run(("git", "status", "--porcelain=v1"), cwd=repo, text=True, capture_output=True, check=True).stdout
        binding_before = dict(ledger.connection.execute("SELECT repository_path,starting_sha,canonical_sha FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone())
        self.assertTrue(before)
        self.assertNotEqual(binding_before["starting_sha"], subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip())
        result = enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        self.assertEqual(result["status"], "finalized")
        self.assertEqual(dict(ledger.connection.execute("SELECT repository_path,starting_sha,canonical_sha FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()), binding_before)
        self.assertEqual(subprocess.run(("git", "status", "--porcelain=v1"), cwd=repo, text=True, capture_output=True, check=True).stdout, before)
        self.assertEqual(config_path.read_bytes(), base64.b64decode(prepared["document"]["new_config_bytes"]))
        ledger.close(); tmp.cleanup()

    def test_binding_commit_and_repository_guards_reject_forgery(self) -> None:
        cases = (
            ("starting_sha", ""),
            ("starting_sha", "f" * 40),
            ("canonical_sha", ""),
            ("canonical_sha", "f" * 40),
            ("repository_path", "/tmp/not-the-registered-repository"),
            ("ownership_verified", 0),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
                ledger.connection.execute(f"UPDATE runtime_bindings SET {field}=? WHERE ticket_id=?", (value, ticket))
                ledger.connection.commit()
                with self.assertRaises(ValueError):
                    _eligible(ledger, (ticket,), root / "repo")
                ledger.close(); tmp.cleanup()

    def test_binding_repository_identity_is_lexical_and_no_follow(self) -> None:
        cases = ("relative", "normalized", "direct_symlink", "nested_component_symlink")
        for case in cases:
            with self.subTest(case=case):
                tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
                repo = root / "repo"
                if case == "relative":
                    forged = "repo"
                elif case == "normalized":
                    forged = str(repo) + "/."
                elif case == "direct_symlink":
                    alias = root / "repo-alias"; alias.symlink_to(repo); forged = str(alias)
                else:
                    real_parent = root / "real-parent"; real_parent.mkdir()
                    moved = real_parent / "repo"; repo.rename(moved)
                    alias_parent = root / "alias-parent"; alias_parent.symlink_to(real_parent)
                    forged = str(alias_parent / "repo")
                ledger.connection.execute("UPDATE runtime_bindings SET repository_path=? WHERE ticket_id=?", (forged, ticket))
                ledger.connection.commit()
                with self.assertRaises(ValueError):
                    _eligible(ledger, (ticket,), repo)
                ledger.close(); tmp.cleanup()

    def test_repository_replacement_during_commit_validation_fails_closed(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        repo = root / "repo"
        original = repo
        replacement = root / "replacement"
        replacement.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=replacement, check=True)
        (replacement / "README").write_text("replacement\n")
        subprocess.run(("git", "add", "README"), cwd=replacement, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "replacement"), cwd=replacement, check=True)
        from unittest.mock import patch
        import local_first_orchestrator.signer_enrollment as enrollment
        real_bound_commit = enrollment._bound_commit
        replaced = False
        def replace_before_validation(repository, value, field):
            nonlocal replaced
            if not replaced:
                replaced = True
                original.rename(root / "original")
                original.symlink_to(replacement)
            return real_bound_commit(repository, value, field)
        with patch.object(enrollment, "_bound_commit", replace_before_validation):
            with self.assertRaises(ValueError):
                _eligible(ledger, (ticket,), repo)
        ledger.close(); tmp.cleanup()

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

    def test_valid_finalized_enrollment_advances_to_legacy_release_revalidation(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
        self.assertIsNotNone(blocker)
        self.assertNotIn("signer enrollment reconciliation required", blocker["reason"])
        self.assertIn("legacy", blocker["reason"])
        ledger.connection.execute("UPDATE controller_state SET paused=0 WHERE id=1")
        ledger.connection.commit()
        preview = preview_next(ledger, signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, signer_config_path=config_path)
        self.assertEqual(preview.next_stage, "reconciliation_required")
        self.assertNotIn("signer enrollment reconciliation required", preview.blocker_reason or "")
        ledger.close(); tmp.cleanup()

    def test_ticket_scoped_migration_ignores_unrelated_corrupt_signer_enrollment(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        unrelated = ledger.create_ticket(title="unrelated", state=CanonicalState.DRAFT, external_id="external-unrelated", contract={"objective":"u","criterion_ids":["AC-1"],"primary_symbol":"u","allowed_files":["README"],"forbidden_changes":[],"patch_budget":{"max_files":1,"max_changed_lines":1},"verification":{"commands":[]},"risk":"low","review_required":True,"max_attempts":1,"dependencies":[]})
        ledger.connection.execute("DROP TRIGGER runtime_signer_enrollment_intents_immutable_identity")
        ledger.connection.execute("UPDATE runtime_signer_enrollment_intents SET reason='forged-unrelated-corruption'")
        ledger.connection.commit()

        unscoped = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
        scoped_unrelated = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path, ticket_id=unrelated)
        scoped_original = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path, ticket_id=ticket)

        self.assertIsNotNone(unscoped)
        self.assertTrue(unscoped["reason"].startswith("signer enrollment reconciliation required:"))
        self.assertIsNone(scoped_unrelated)
        self.assertIsNotNone(scoped_original)
        self.assertTrue(scoped_original["reason"].startswith("signer enrollment reconciliation required:"))
        ledger.close(); tmp.cleanup()

    def test_completion_event_document_and_signature_mutations_stop_enrollment(self) -> None:
        for field, value in (("document_json", "{}"), ("detached_signature", base64.b64encode(b"x" * 64).decode())):
            tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
            ledger.connection.execute("DROP TRIGGER events_immutable_update")
            event = ledger.connection.execute("SELECT id,payload_json FROM events WHERE event_type='runtime_signer_enrollment_completed'").fetchone()
            payload = json.loads(event["payload_json"])
            payload[field] = value
            ledger.connection.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload, sort_keys=True, separators=(",", ":")), event["id"]))
            ledger.connection.execute("UPDATE controller_state SET paused=0 WHERE id=1")
            ledger.connection.commit()
            blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
            self.assertIsNotNone(blocker, field)
            self.assertTrue(blocker["reason"].startswith("signer enrollment reconciliation required:"), field)
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
    def test_detector_stops_on_every_mutable_intent_authority_field(self) -> None:
        fields = {
            "document_json": "{}", "document_hash": "f" * 64, "detached_signature": base64.b64encode(b"x" * 64).decode(),
            "ledger_identity": "/tmp/forged-ledger.db", "nonce": "forged-nonce", "operator_id": "forged-operator",
            "reason": "forged-reason", "ticket_ids_json": "[]", "public_key_fingerprint": "f" * 64,
            "authority_hash": "f" * 64, "old_config_hash": "f" * 64, "new_config_hash": "f" * 64,
            "new_config_bytes": base64.b64encode(b"forged").decode(), "selected_bindings_json": "{}",
            "old_config_identity_json": "{}",
        }
        for field, value in fields.items():
            tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
            enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
            ledger.connection.executescript("DROP TRIGGER runtime_signer_enrollment_intents_immutable_identity;")
            ledger.connection.execute(f"UPDATE runtime_signer_enrollment_intents SET {field}=?", (value,))
            ledger.connection.execute("UPDATE controller_state SET paused=0 WHERE id=1")
            ledger.connection.commit()
            before = ledger.connection.execute("SELECT operator_signer_fingerprint,operator_authority_hash FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone()
            blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
            self.assertIsNotNone(blocker, field)
            self.assertTrue(blocker["reason"].startswith("signer enrollment reconciliation required:"), field)
            self.assertEqual(preview_next(ledger, signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, signer_config_path=config_path).next_stage, "reconciliation_required")
            class NoSideEffectsBoard:
                timeout_seconds = 1
            with self.assertRaisesRegex(RuntimeError, "signer enrollment reconciliation required"):
                ProcessNextScheduler(ledger, NoSideEffectsBoard(), worker_id="fixture", lease_seconds=30, clock=lambda: 100, native_dependency_release_signer_public_key=base64.b64decode(public_b64), native_dependency_release_signer_fingerprint=fingerprint, native_dependency_release_signer_config_path=config_path).process_next()
            self.assertEqual(ledger.connection.execute("SELECT operator_signer_fingerprint,operator_authority_hash FROM runtime_bindings WHERE ticket_id=?", (ticket,)).fetchone(), before)
            ledger.close(); tmp.cleanup()

    def test_detector_derives_binding_authority_hash_not_evidence_hash(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        forged = "f" * 64
        ledger.connection.executescript("DROP TRIGGER runtime_signer_enrollments_immutable_update; DROP TRIGGER runtime_bindings_signer_immutable;")
        ledger.connection.execute("UPDATE runtime_bindings SET operator_authority_hash=? WHERE ticket_id=?", (forged, ticket))
        ledger.connection.execute("UPDATE runtime_signer_enrollments SET new_binding_identity_json=json_set(new_binding_identity_json,'$.operator_authority_hash',?), authority_hash=? WHERE ticket_id=?", (forged, forged, ticket))
        ledger.connection.commit()
        blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
        self.assertIsNotNone(blocker)
        self.assertTrue(blocker["reason"].startswith("signer enrollment reconciliation required:"))
        ledger.close(); tmp.cleanup()

    def test_detector_stops_on_post_enrollment_repository_replacement(self) -> None:
        tmp, root, ledger, config_path, prepared, signature, public_b64, fingerprint, ticket = self._enrollment_fixture()
        enroll_operator_signer(ledger, config_path=config_path, document=prepared["document"], detached_signature=signature, public_key_b64=public_b64, fingerprint=fingerprint)
        repo = root / "repo"
        replacement = root / "replacement"
        repo.rename(root / "original")
        replacement.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=replacement, check=True)
        (replacement / "README").write_text("replacement\n")
        subprocess.run(("git", "add", "README"), cwd=replacement, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "replacement"), cwd=replacement, check=True)
        repo.symlink_to(replacement)
        blocker = ledger.native_dependency_release_migration_required(signer_public_key=base64.b64decode(public_b64), signer_fingerprint=fingerprint, config_path=config_path)
        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertTrue(blocker["reason"].startswith("signer enrollment reconciliation required:"))
        ledger.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
