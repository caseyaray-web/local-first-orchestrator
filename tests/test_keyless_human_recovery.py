from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class KeylessHumanRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(("git", "add", "README"), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.commit = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.ledger_path = self.root / "ledger.db"; self.ledger = Ledger(self.ledger_path); self.ledger.migrate(); self.addCleanup(self.ledger.close)
        generated_ticket = self.ledger.create_ticket(title="only ticket", state=CanonicalState.READY_LOCAL, external_id="fixture", contract={"objective":"x","criterion_ids":["AC"],"primary_symbol":"x","allowed_files":["README"],"forbidden_changes":[],"patch_budget":{"max_files":1,"max_changed_lines":1},"verification":{"commands":[]},"risk":"low","review_required":True,"max_attempts":1,"dependencies":[]})
        self.ticket = "C12R1-TK-3"
        self.ledger.connection.execute("UPDATE tickets SET id=? WHERE id=?", (self.ticket, generated_ticket)); self.ledger.connection.commit()
        self.ledger.bind_runtime(self.ticket, str(self.repo), self.commit)
        with self.ledger._transaction() as conn:
            event = self.ledger._append_event(conn, entity_type="ticket", entity_id=self.ticket, event_type="generated_microticket_created", actor_id="fixture", to_state="draft", payload={"ticket_id":self.ticket})
            conn.execute("INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,acknowledged_at,external_task_id,operation) VALUES (?,?,?,'{}',?,1,1,?,'create_microticket')", (self.ticket,event,"draft","fixture-create","fixture"))
            conn.execute("INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,1)", (self.ticket,"graph","fixture","parent","{}","ready"))
        self.config = self.root / "operator.json"
        self.config.write_text(json.dumps({"ledger_path":str(self.ledger_path),"canonical_repository":str(self.repo),"repository_allowlist":[str(self.repo)],"implementation":{"profile":"i","provider":"p","model":"m"},"review":{"profile":"r","provider":"p","model":"m"},"worktree_root":str(self.root / "worktrees"),"artifact_root":str(self.root / "artifacts"),"implementation_timeout_seconds":30,"review_timeout_seconds":30}) + "\n")
        self.ledger.pause("fixture", reason="paused")

    def _runtime(self):
        from local_first_orchestrator.keyless_human_recovery import HelperRuntime, SourcePrerequisite
        exchange = self.root / "exchange"; exchange.mkdir(exist_ok=True)
        return HelperRuntime(ledger_path=self.ledger_path, config_path=self.config, installed_source=SourcePrerequisite(Path("/root/snapshot"), Path("/usr/bin/python3").resolve()), key_path=self.root / "private" / "root-only.key", actor="ocadmin", exchange_parent=exchange)

    def test_isolated_system_python_can_import_reviewed_source(self) -> None:
        from dataclasses import replace
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery, SourcePrerequisite
        runtime = self._runtime()
        runtime = replace(runtime, installed_source=SourcePrerequisite(Path(__file__).resolve().parents[1], runtime.installed_source.python_executable))
        argv = KeylessHumanRecovery(runtime)._argv("--help")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepare-stale-routing-recovery", result.stdout)

    def test_real_isolated_cli_prepares_and_enrolls_fixture_without_board_effect(self) -> None:
        from dataclasses import replace
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery, SourcePrerequisite
        runtime = self._runtime()
        runtime = replace(runtime, installed_source=SourcePrerequisite(Path(__file__).resolve().parents[1], runtime.installed_source.python_executable))
        def runner(argv, *, uid, gid):
            self.assertEqual((uid, gid), (os.getuid(), os.getgid()))
            process = subprocess.run(argv, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            return process.stdout
        helper = KeylessHumanRecovery(runtime, runner=runner, confirmer=lambda _: True,
            identity=lambda: (0, True, True, os.getuid(), os.getgid()), source_validator=lambda _: None)
        before = self.config.read_bytes()
        prepared = helper.prepare_enrollment()
        self.assertEqual(self.config.read_bytes(), before)
        helper.confirm_and_enroll(prepared)
        self.assertEqual(self.ledger.connection.execute("SELECT operator_signer_fingerprint FROM runtime_bindings WHERE ticket_id=?", (self.ticket,)).fetchone()[0], prepared.fingerprint)
        self.assertEqual(self.ledger.connection.execute("SELECT count(*) FROM board_projection_outbox WHERE ticket_id=?", (self.ticket,)).fetchone()[0], 1)

    def test_prepare_is_exactly_scoped_and_never_uses_sudo_or_board(self) -> None:
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery
        calls = []
        def runner(argv, *, uid, gid):
            calls.append((argv, uid, gid))
            output = Path(argv[argv.index("--output-file") + 1])
            output.write_bytes(b'{"canonical":"enrollment"}')
            return "{}"
        helper = KeylessHumanRecovery(self._runtime(), runner=runner, confirmer=lambda _: True, identity=lambda: (0, True, True, 1000, 1000), source_validator=lambda _: None)
        prepared = helper.prepare_enrollment()
        self.assertEqual(prepared.ticket_id, self.ticket)
        self.assertEqual(prepared.document_hash, hashlib.sha256(b'{"canonical":"enrollment"}').hexdigest())
        argv, uid, gid = calls[0]
        self.assertNotIn("sudo", argv); self.assertNotIn("--allow-board-writes", argv)
        self.assertEqual(uid, 1000); self.assertEqual(gid, 1000)
        self.assertEqual(argv[argv.index("--ticket-id") + 1], self.ticket)
        self.assertNotIn("dispatch", " ".join(argv).lower())

    def test_confirm_sign_then_enroll_runs_only_as_config_owner_and_hides_private_key(self) -> None:
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery
        calls = []
        def runner(argv, *, uid, gid):
            calls.append(argv)
            output = Path(argv[argv.index("--output-file") + 1]) if "--output-file" in argv else None
            if output: output.write_bytes(b'{"canonical":"enrollment"}')
            return "{}"
        transcript = io.StringIO()
        helper = KeylessHumanRecovery(self._runtime(), runner=runner, confirmer=lambda message: (transcript.write(message), True)[1], identity=lambda: (0, True, True, 1000, 1000), source_validator=lambda _: None)
        prepared = helper.prepare_enrollment()
        with patch("local_first_orchestrator.keyless_human_recovery.os.chown", wraps=os.chown) as chown:
            helper.confirm_and_enroll(prepared)
        signature = prepared.document_path.with_name("enrollment.sig")
        self.assertIn((signature, 1000, 1000), [call.args for call in chown.call_args_list])
        self.assertEqual(len(calls), 2)
        self.assertIn("enroll-operator-signer", calls[1])
        self.assertIn(prepared.document_hash, transcript.getvalue())
        self.assertNotIn("BEGIN PRIVATE", transcript.getvalue())
        self.assertNotIn((self.root / "private" / "root-only.key").read_bytes().hex(), transcript.getvalue())
        self.assertEqual(stat.S_IMODE((self.root / "private" / "root-only.key").stat().st_mode), 0o600)

    def test_rejects_wrong_ticket_no_tty_no_sudo_and_source_drift_before_side_effect(self) -> None:
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery
        runtime = self._runtime()
        for identity in ((0, False, True, 1000, 1000), (0, True, False, 1000, 1000), (1000, True, True, 1000, 1000)):
            with self.subTest(identity=identity):
                with self.assertRaises(PermissionError):
                    KeylessHumanRecovery(runtime, runner=lambda *a, **k: self.fail("runner"), confirmer=lambda _: True, identity=lambda: identity, source_validator=lambda _: None).prepare_enrollment()
        with self.assertRaises(ValueError):
            KeylessHumanRecovery(runtime, runner=lambda *a, **k: self.fail("runner"), confirmer=lambda _: True, identity=lambda: (0, True, True, 1000, 1000), source_validator=lambda _: None).prepare_enrollment(ticket_id="other")
        with self.assertRaises(PermissionError):
            KeylessHumanRecovery(runtime, runner=lambda *a, **k: self.fail("runner"), confirmer=lambda _: True, identity=lambda: (0, True, True, 1, 1), source_validator=lambda _: (_ for _ in ()).throw(PermissionError("source drift"))).prepare_enrollment()

    def test_config_drift_and_crash_boundaries_fail_closed_without_reconfirmation_or_private_key_exposure(self) -> None:
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery
        calls = []
        def runner(argv, *, uid, gid):
            calls.append(argv)
            output = Path(argv[argv.index("--output-file") + 1]) if "--output-file" in argv else None
            if output: output.write_bytes(b'{"canonical":"enrollment"}')
            if "enroll-operator-signer" in argv: raise RuntimeError("crash after root signing")
            return "{}"
        confirms = []
        helper = KeylessHumanRecovery(self._runtime(), runner=runner, confirmer=lambda message: confirms.append(message) or True, identity=lambda: (0, True, True, 1000, 1000), source_validator=lambda _: None)
        prepared = helper.prepare_enrollment()
        self.config.write_bytes(self.config.read_bytes() + b" ")
        with self.assertRaises(RuntimeError): helper.confirm_and_enroll(prepared)
        self.assertEqual(len(confirms), 0)  # drift stops before a confirmation can authorize changed bytes
        self.assertEqual(len(calls), 1)  # no write call after prepared config drift
        self.assertFalse((self.root / "private" / "root-only.key").read_bytes() in b"".join(" ".join(c).encode() for c in calls))

    def test_stale_recovery_is_separately_confirmed_and_fixed_to_attempt_two(self) -> None:
        from local_first_orchestrator.keyless_human_recovery import KeylessHumanRecovery
        calls, confirmations = [], []
        def runner(argv, *, uid, gid):
            calls.append(argv)
            if "--output-file" in argv:
                Path(argv[argv.index("--output-file") + 1]).write_bytes(b'{"canonical":"recovery"}')
            return "{}"
        helper = KeylessHumanRecovery(self._runtime(), runner=runner, confirmer=lambda text: confirmations.append(text) or True, identity=lambda: (0, True, True, 1000, 1000), source_validator=lambda _: None)
        prepared = helper.prepare_stale_routing_recovery()
        helper.confirm_and_recover_stale_routing(prepared)
        self.assertEqual(len(confirmations), 1)
        self.assertIn("RECOVER C12R1-TK-3 attempt 2", confirmations[0])
        self.assertEqual(calls[0][calls[0].index("--task-id") + 1], "C12R1-TK-3")
        self.assertEqual(calls[0][calls[0].index("--attempt-number") + 1], "2")
        self.assertIn("recover-stale-routing", calls[1])
        self.assertNotIn("--allow-board-writes", calls[1])


if __name__ == "__main__":
    unittest.main()
