from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.cli import _ad_hoc_controller, _registered_controller, main as cli_main, register_cli
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.generated_projection import GeneratedProjectionDeliveryResult
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, save_operator_config
from local_first_orchestrator.states import CanonicalState


class RegisteredRuntimeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=self.root/"repo"; self.repo.mkdir()
        subprocess.run(("git","init","-q"),cwd=self.repo,check=True)
        self.database=self.root/"ledger.db"; self.ledger=Ledger(self.database); self.ledger.migrate()
        self.config_path=self.root/"operator.json"
        save_operator_config(OperatorConfig(self.database,self.repo,(self.repo,),ModelRegistration("registered-profile","registered-provider","registered-model"),ModelRegistration("review","review-provider","review-model"),self.root/"registered-worktrees",self.root/"registered-artifacts",1800,900),self.config_path)
    def tearDown(self) -> None: self.ledger.close(); self.temp.cleanup()
    def args(self, **changes: object) -> argparse.Namespace:
        value={"repository":".","allow_repository":[],"worktree_root":None,"artifact_root":None,"implementation_timeout_seconds":None,"review_timeout_seconds":None,"operator_config_path":str(self.config_path),"ad_hoc_runtime":False}
        value.update(changes); return argparse.Namespace(**value)
    def test_registered_runtime_uses_authoritative_paths_identity_and_timeout(self) -> None:
        controller, config=_registered_controller(self.ledger,self.args())
        self.assertEqual(controller.config.worktree_root,(self.root/"registered-worktrees").resolve()); self.assertEqual(controller.config.artifact_root,(self.root/"registered-artifacts").resolve()); self.assertEqual(controller.config.implementation_timeout_seconds,1800)
        self.assertEqual(controller.local_model.implementation_timeout_seconds,1800); self.assertEqual(controller.local_model.provider,"registered-provider"); self.assertEqual(controller.local_model.model,"registered-model"); self.assertEqual(config.canonical_repository,self.repo.resolve())
        self.assertEqual(controller.config.review_timeout_seconds,900); self.assertEqual(controller.local_model.review_timeout_seconds,900)
    def test_registered_runtime_rejects_cli_override_instead_of_falling_back(self) -> None:
        with self.assertRaisesRegex(ValueError,"forbids runtime overrides"): _registered_controller(self.ledger,self.args(implementation_timeout_seconds=300))
    def test_ad_hoc_runtime_is_explicit_and_has_separate_default(self) -> None:
        controller=_ad_hoc_controller(self.ledger,self.args(repository=str(self.repo),operator_config_path=None,ad_hoc_runtime=True))
        self.assertEqual(controller.config.implementation_timeout_seconds,300); self.assertNotEqual(controller.config.worktree_root,(self.root/"registered-worktrees").resolve())

    def test_board_access_is_explicitly_available_to_import_and_registered_execution(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        for command, tail in (("import", ["--task-id", "card"]), ("run-once", ["--task-id", "ticket"])):
            args = parser.parse_args(["--database", str(self.database), "--board", "board", "--hermes-executable", "hermes", command, *tail])
            self.assertEqual(args.board, "board"); self.assertEqual(args.hermes_executable, "hermes")

    def test_daemon_cli_is_explicit_bounded_and_fail_closed(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        args = parser.parse_args([
            "--database", str(self.database),
            "daemon",
            "--max-iterations", "3",
            "--idle-sleep-seconds", "2",
            "--busy-sleep-seconds", "0.5",
            "--error-backoff-seconds", "1.5",
            "--max-error-backoff-seconds", "9",
        ])
        self.assertEqual(args.command, "daemon")
        self.assertFalse(args.execute)
        self.assertFalse(args.allow_board_writes)
        self.assertEqual(args.max_iterations, 3)
        self.assertEqual(args.idle_sleep_seconds, 2.0)
        self.assertEqual(args.busy_sleep_seconds, 0.5)
        self.assertEqual(args.error_backoff_seconds, 1.5)
        self.assertEqual(args.max_error_backoff_seconds, 9.0)

        with self.assertRaisesRegex(PermissionError, "daemon requires --execute"):
            cli_main(["--database", str(self.database), "daemon", "--max-iterations", "0"])
        with self.assertRaisesRegex(PermissionError, "requires --allow-board-writes"):
            cli_main(["--database", str(self.database), "daemon", "--execute", "--max-iterations", "0"])
        with self.assertRaisesRegex(ValueError, "requires --hermes-executable and --board"):
            cli_main([
                "--database", str(self.database),
                "daemon", "--execute", "--allow-board-writes", "--max-iterations", "0",
            ])

    def test_hermes_execution_reconciliation_cli_is_explicit_and_read_only(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        args = parser.parse_args([
            "--database", str(self.database),
            "reconcile-hermes-execution", "--task-id", "H-1", "--run-id", "7",
        ])
        self.assertEqual(args.command, "reconcile-hermes-execution")
        self.assertEqual(args.task_id, "H-1")
        self.assertEqual(args.run_id, 7)
        self.assertFalse(hasattr(args, "allow_board_writes"))
        with self.assertRaisesRegex(ValueError, "requires --hermes-executable and --board"):
            cli_main([
                "--database", str(self.database),
                "reconcile-hermes-execution", "--task-id", "H-1", "--run-id", "7",
            ])

    def test_projection_recovery_cli_is_explicit_operator_only_and_read_only(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        args = parser.parse_args([
            "--database", str(self.database),
            "recover-generated-projection", "--task-id", "TK-1", "--event-id", "7",
            "--operator-id", "casey", "--reason", "pre-native replacement",
        ])
        self.assertEqual(args.command, "recover-generated-projection")
        self.assertEqual(args.task_id, "TK-1")
        self.assertEqual(args.event_id, 7)
        self.assertEqual(args.operator_id, "casey")
        self.assertFalse(hasattr(args, "allow_board_writes"))
        with self.assertRaisesRegex(ValueError, "requires --hermes-executable and --board"):
            cli_main([
                "--database", str(self.database),
                "recover-generated-projection", "--task-id", "TK-1", "--event-id", "7",
                "--operator-id", "casey", "--reason", "pre-native replacement",
            ])

    def test_native_release_revalidation_is_registered_read_only_operation(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        args = parser.parse_args([
            "--database", str(self.database), "--operator-config-path", str(self.config_path),
            "--hermes-executable", "/bin/true", "--board", "isolated",
            "revalidate-native-release", "--task-id", "TK-1", "--operator-id", "casey", "--reason", "legacy proof",
        ])
        self.assertEqual(args.command, "revalidate-native-release")
        self.assertEqual((args.task_id, args.operator_id, args.reason), ("TK-1", "casey", "legacy proof"))
        with self.assertRaisesRegex(ValueError, "requires registered operator runtime"):
            cli_main([
                "--database", str(self.database), "--ad-hoc-runtime", "--hermes-executable", "/bin/true", "--board", "isolated",
                "revalidate-native-release", "--task-id", "TK-1", "--operator-id", "casey", "--reason", "legacy proof",
            ])

    def test_operator_lifecycle_cli_pause_resume_and_status_are_durable(self) -> None:
        paused = io.StringIO()
        with contextlib.redirect_stdout(paused):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "pause", "--reason", "operator maintenance", "--operator-id", "operator",
            ]), 0)
        self.assertTrue(json.loads(paused.getvalue())["paused"])
        self.assertTrue(self.ledger.status()["paused"])

        status = io.StringIO()
        with contextlib.redirect_stdout(status):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "operator-status", "--limit", "10",
            ]), 0)
        payload = json.loads(status.getvalue())
        self.assertTrue(payload["paused"])
        self.assertIn("scheduler_detail", payload)
        self.assertIn("outbox_pending", payload)

        resumed = io.StringIO()
        with contextlib.redirect_stdout(resumed):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "resume", "--reason", "maintenance complete", "--operator-id", "operator",
            ]), 0)
        self.assertFalse(json.loads(resumed.getvalue())["paused"])
        self.assertFalse(self.ledger.status()["paused"])
        events = [row["event_type"] for row in self.ledger.connection.execute(
            "SELECT event_type FROM events WHERE entity_type='controller' ORDER BY id"
        )]
        self.assertEqual(events[-2:], ["paused", "resumed"])

    def test_reopen_terminal_cli_requires_pause_and_reopens_budget_exhaustion_terminal(self) -> None:
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F-reopen-cli','feature','active',?,?)",
            (now, now),
        )
        ticket = self.ledger.create_ticket(title="terminal-cli", state=CanonicalState.NEEDS_TRIAGE)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F-reopen-cli' WHERE id=?", (ticket,))
        notice = self.ledger.record_terminal_unresolvable(
            ticket,
            attempt_number=1,
            failure_fingerprint="f" * 64,
            reason="paid escalation budget exhausted",
            summary={"local_feedback": "terminal evidence"},
            notification_target="mattermost:ops",
        )
        self.ledger.connection.execute(
            "INSERT INTO paid_budgets(feature_id,architecture_limit,checkpoint_limit,escalation_limit,created_at,updated_at) VALUES ('F-reopen-cli',0,0,1,?,?)",
            (now, now),
        )
        with self.assertRaisesRegex(PermissionError, "paused controller"):
            cli_main([
                "--database", str(self.database),
                "reopen-terminal", "--ticket-id", ticket, "--reason", "budget added", "--operator-id", "operator",
            ])
        cli_main(["--database", str(self.database), "pause", "--reason", "terminal recovery", "--operator-id", "operator"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "reopen-terminal", "--ticket-id", ticket, "--reason", "paid capacity added", "--operator-id", "operator",
            ]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual((payload["ticket_id"], payload["state"], payload["remaining_escalation_calls"]), (ticket, "needs_triage", 1))
        notice_row = self.ledger.connection.execute(
            "SELECT status,superseded_at FROM gateway_notification_outbox WHERE operation_id=?", (notice["operation_id"],)
        ).fetchone()
        self.assertEqual(notice_row["status"], "superseded")
        self.assertIsNotNone(notice_row["superseded_at"])

    def test_resolve_terminal_retry_local_cli_grants_budget_and_returns_repairing(self) -> None:
        ticket = self.ledger.create_ticket(title="terminal-local-budget", state=CanonicalState.NEEDS_TRIAGE)
        worktree = self.root / "terminal-local-worktree"
        worktree.mkdir()
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,post_diff_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (ticket, 1, "a" * 40, "wt/terminal", str(worktree), "b" * 64, "c" * 64, 1),
        )
        terminal = self.ledger.record_terminal_unresolvable(
            ticket,
            attempt_number=1,
            failure_fingerprint="c" * 64,
            reason="manual intervention required",
            summary={"local_feedback": "terminal evidence"},
            notification_target="mattermost:ops",
        )
        cli_main(["--database", str(self.database), "pause", "--reason", "manual terminal recovery", "--operator-id", "operator"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "resolve-terminal",
                "--ticket-id", ticket,
                "--mode", "retry-local",
                "--additional-local-attempts", "2",
                "--reason", "grant two more local attempts",
                "--operator-id", "operator",
            ]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual((payload["ticket_id"], payload["state"], payload["additional_local_attempts"], payload["new_max_attempts"]), (ticket, "repairing", 2, 4))
        self.assertEqual((self.ledger.get_ticket(ticket)["state"], self.ledger.get_ticket(ticket)["max_attempts"]), ("repairing", 4))
        attempt = self.ledger.connection.execute("SELECT attempt_number,pre_diff_hash FROM attempts WHERE ticket_id=? ORDER BY attempt_number DESC LIMIT 1", (ticket,)).fetchone()
        self.assertEqual((attempt["attempt_number"], attempt["pre_diff_hash"]), (2, "c" * 64))
        notice = self.ledger.connection.execute("SELECT status FROM gateway_notification_outbox WHERE operation_id=?", (terminal["operation_id"],)).fetchone()
        self.assertEqual(notice["status"], "superseded")

    def test_historical_claim_retirement_cli_supports_exact_operator_targets(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        for selector, value in (("--ticket-id", "internal-ticket"), ("--external-id", "t-generated"), ("--claim-id", "claim-1")):
            args = parser.parse_args([
                "--database", str(self.database),
                "retire-historical-claims", selector, value,
                "--reason", "terminal historical residue",
            ])
            self.assertEqual(args.command, "retire-historical-claims")
            self.assertEqual(getattr(args, selector[2:].replace("-", "_")), value)
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "--database", str(self.database),
                "retire-historical-claims", "--reason", "missing selector",
            ])
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "--database", str(self.database),
                "retire-historical-claims", "--ticket-id", "one", "--claim-id", "two",
                "--reason", "ambiguous selector",
            ])

    def test_recovery_status_surfaces_incomplete_invocation_and_exact_inspection_hint(self) -> None:
        ticket = self.ledger.create_ticket(title="recovery")
        self.ledger.start_model_invocation(
            invocation_id="inv-recovery-1",
            ticket_id=ticket,
            attempt_number=1,
            stage="implementation",
            provider="provider",
            model="model",
            packet_hash="packet",
            worktree_path=str(self.root / "worktree"),
            timeout_seconds=60,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--database", str(self.database),
                "recovery-status", "--limit", "10",
            ]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(len(payload["incomplete_model_invocations"]), 1)
        action = payload["recommended_actions"][0]
        self.assertEqual(action["kind"], "incomplete_model_invocation")
        self.assertEqual(action["ticket_id"], ticket)
        self.assertEqual(action["command"], f"inspect --task-id {ticket}")
        self.assertIn("fail closed", action["action"])

    def test_operator_aliases_are_discoverable_and_route_to_same_handlers(self) -> None:
        parser = argparse.ArgumentParser(); register_cli(parser)
        doctor = parser.parse_args(["--database", str(self.database), "doctor"])
        self.assertEqual(doctor.command, "doctor")
        init = parser.parse_args([
            "--database", str(self.database),
            "--repository", str(self.repo),
            "init", "--config-path", str(self.root / "alias-operator.json"),
        ])
        self.assertEqual(init.command, "init")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--database", str(self.database), "doctor"]), 0)
        self.assertIn("recommended_actions", json.loads(output.getvalue()))

    def test_approve_paid_cli_persists_one_purpose_scoped_call(self) -> None:
        self.assertEqual(
            cli_main([
                "--database", str(self.database),
                "approve-paid",
                "--feature-id", "F-paid",
                "--purpose", "integration_checkpoint",
                "--reason", "operator permits one checkpoint call",
                "--idempotency-key", "approval-paid-1",
                "--operator-id", "operator",
            ]),
            0,
        )
        row = self.ledger.connection.execute("SELECT * FROM paid_approvals WHERE idempotency_key='approval-paid-1'").fetchone()
        self.assertEqual((row["feature_id"], row["purpose"], row["calls"], row["actor_id"]), ("F-paid", "integration_checkpoint", 1, "operator"))

    def test_project_generated_uses_registered_route_and_canonical_repository(self) -> None:
        result = GeneratedProjectionDeliveryResult("no_work")
        with mock.patch("local_first_orchestrator.cli.GeneratedProjectionWorker") as worker, mock.patch("local_first_orchestrator.cli.HermesBoardAdapter") as adapter:
            worker.return_value.deliver_one.return_value = result
            adapter.return_value.is_fake = False
            cli_main([
                "--database", str(self.database), "--operator-config-path", str(self.config_path),
                "--hermes-executable", "/bin/true", "--board", "isolated", "project-generated", "--allow-board-writes",
            ])
        kwargs = adapter.call_args.kwargs
        self.assertEqual(kwargs["implementation_profile"], "registered-profile")
        self.assertEqual(kwargs["canonical_repository"], self.repo.resolve())
        self.assertIs(worker.call_args.args[1], adapter.return_value)

    def test_project_generated_rejects_unregistered_real_routing(self) -> None:
        with self.assertRaisesRegex(ValueError, "operator dashboard is not registered"):
            cli_main([
                "--database", str(self.database), "--operator-config-path", str(self.root / "missing.json"),
                "--hermes-executable", "/bin/true", "--board", "isolated", "project-generated", "--allow-board-writes",
            ])

if __name__ == "__main__": unittest.main()
