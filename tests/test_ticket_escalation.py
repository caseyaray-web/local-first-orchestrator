from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.gateway_notification import HermesGatewayNotificationWorker
from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler
from local_first_orchestrator.states import CanonicalState


class Board:
    timeout_seconds = 1

    def find_comment_marker(self, *_args, **_kwargs):
        return "not_found"

    def deliver_comment(self, *_args, **_kwargs):
        return None

    def set_state(self, *_args, **_kwargs):
        return None

    def create_microticket(self, *_args, **_kwargs):
        return "generated"


class TicketEscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(
            title="repair me",
            state=CanonicalState.NEEDS_TRIAGE,
            external_id="H-1",
            contract={"max_attempts": 2},
        )
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,post_diff_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (self.ticket, 2, "a" * 40, "wt/t", str(self.root / "worktree"), "b" * 64, "c" * 64, 1),
        )
        self.failure = {
            "action": "triage",
            "ticket_id": self.ticket,
            "attempt_number": 2,
            "failure_fingerprint": "d" * 64,
            "failure_evidence": "changed path outside allowlist; changed line budget exceeded",
            "source": "validation",
        }
        self.ledger.record_runtime_stage(
            self.ticket,
            "repair-routing-2",
            json.dumps(self.failure, sort_keys=True, separators=(",", ":")),
            attempt_number=2,
            base_sha="a" * 40,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_paid_repair_authorization_creates_exact_post_budget_attempt(self) -> None:
        result = self.ledger.apply_paid_repair_authorization(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            feedback="Deterministic findings.\nPaid guidance: keep scope narrow.",
            response={"decision": "repair", "feedback": "keep scope narrow"},
        )
        self.assertEqual(result["next_attempt_number"], 3)
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.REPAIRING.value)
        attempt = self.ledger.connection.execute(
            "SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=3", (self.ticket,)
        ).fetchone()
        self.assertEqual(attempt["pre_diff_hash"], "c" * 64)
        self.assertEqual(attempt["worktree_path"], str(self.root / "worktree"))
        prior = self.ledger.connection.execute(
            "SELECT outcome,failure_fingerprint FROM attempts WHERE ticket_id=? AND attempt_number=2", (self.ticket,)
        ).fetchone()
        self.assertEqual((prior["outcome"], prior["failure_fingerprint"]), ("repair_requested", "d" * 64))
        transition = self.ledger.connection.execute(
            "SELECT event_type,from_state,to_state FROM events WHERE entity_id=? AND event_type='state_transition' ORDER BY id DESC LIMIT 1",
            (self.ticket,),
        ).fetchone()
        self.assertEqual((transition["from_state"], transition["to_state"]), ("needs_triage", "repairing"))

    def test_terminal_unresolvable_is_idempotent_and_enqueues_gateway_notification(self) -> None:
        summary = {"local_feedback": "Fixes exceeded the authorized envelope.", "policy": "budgets_exhausted"}
        first = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="paid escalation budget exhausted",
            summary=summary,
            notification_target="telegram",
        )
        second = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="paid escalation budget exhausted",
            summary=summary,
            notification_target="telegram",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.BLOCKED.value)
        row = self.ledger.connection.execute("SELECT * FROM gateway_notification_outbox WHERE ticket_id=?", (self.ticket,)).fetchone()
        self.assertEqual((row["status"], row["target"], row["failure_fingerprint"]), ("pending", "telegram", "d" * 64))
        self.assertIn("Fixes exceeded the authorized envelope", row["payload"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM terminal_ticket_failures WHERE ticket_id=?", (self.ticket,)).fetchone()[0], 1)

    def test_budget_exhaustion_terminal_can_be_reopened_after_budget_added_and_reterminal_alerts_again(self) -> None:
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F-reopen','feature','active',?,?)",
            (now, now),
        )
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F-reopen' WHERE id=?", (self.ticket,))
        summary = {"local_feedback": "authoritative findings", "policy": "local_and_paid_budgets_exhausted"}
        first = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="paid escalation budget exhausted",
            summary=summary,
            notification_target="mattermost:ops",
        )
        self.ledger.connection.execute(
            "INSERT INTO paid_budgets(feature_id,architecture_limit,checkpoint_limit,escalation_limit,created_at,updated_at) VALUES ('F-reopen',0,0,1,?,?)",
            (now, now),
        )
        with self.assertRaisesRegex(PermissionError, "paused controller"):
            self.ledger.reopen_terminal_ticket_after_paid_budget(
                self.ticket, operator_id="operator", reason="budget added", now=now + 1
            )
        self.ledger.pause("operator", reason="reopen budget-exhaustion terminal")
        reopened = self.ledger.reopen_terminal_ticket_after_paid_budget(
            self.ticket, operator_id="operator", reason="paid escalation capacity added", now=now + 2
        )
        self.assertEqual(reopened["state"], "needs_triage")
        self.assertEqual(reopened["remaining_escalation_calls"], 1)
        terminal = self.ledger.connection.execute(
            "SELECT resolved_at,resolved_by,generation FROM terminal_ticket_failures WHERE ticket_id=?", (self.ticket,)
        ).fetchone()
        self.assertIsNotNone(terminal["resolved_at"])
        self.assertEqual((terminal["resolved_by"], terminal["generation"]), ("operator", 1))
        self.assertIsNotNone(self.ledger.ticket_paid_escalation_candidate(ticket_id=self.ticket))
        first_notice = self.ledger.connection.execute(
            "SELECT status,superseded_at,failure_fingerprint,terminal_generation FROM gateway_notification_outbox WHERE operation_id=?",
            (first["operation_id"],),
        ).fetchone()
        self.assertEqual(first_notice["status"], "superseded")
        self.assertIsNotNone(first_notice["superseded_at"])
        self.assertEqual((first_notice["failure_fingerprint"], first_notice["terminal_generation"]), ("d" * 64, 1))

        second = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="paid escalation budget exhausted",
            summary=summary,
            notification_target="mattermost:ops",
        )
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(
            self.ledger.connection.execute("SELECT COUNT(*) FROM gateway_notification_outbox WHERE ticket_id=?", (self.ticket,)).fetchone()[0],
            2,
        )
        second_notice = self.ledger.connection.execute(
            "SELECT failure_fingerprint,terminal_generation,status FROM gateway_notification_outbox WHERE operation_id=?",
            (second["operation_id"],),
        ).fetchone()
        self.assertEqual((second_notice["failure_fingerprint"], second_notice["terminal_generation"], second_notice["status"]), ("d" * 64, 2, "pending"))
        terminal = self.ledger.connection.execute(
            "SELECT resolved_at,generation FROM terminal_ticket_failures WHERE ticket_id=?", (self.ticket,)
        ).fetchone()
        self.assertIsNone(terminal["resolved_at"])
        self.assertEqual(terminal["generation"], 2)

    def test_reopen_blocks_when_terminal_notification_delivery_is_ambiguous(self) -> None:
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F-ambiguous','feature','active',?,?)",
            (now, now),
        )
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F-ambiguous' WHERE id=?", (self.ticket,))
        notice = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="e" * 64,
            reason="paid escalation budget exhausted",
            summary={"local_feedback": "authoritative findings"},
            notification_target="mattermost:ops",
        )
        self.ledger.connection.execute(
            "INSERT INTO paid_budgets(feature_id,architecture_limit,checkpoint_limit,escalation_limit,created_at,updated_at) VALUES ('F-ambiguous',0,0,1,?,?)",
            (now, now),
        )
        claimed = self.ledger.claim_next_gateway_notification("notify", lease_seconds=60, now=now + 1)
        self.assertEqual(claimed["operation_id"], notice["operation_id"])
        self.ledger.pause("operator", reason="attempt guarded reopen")
        with self.assertRaisesRegex(ValueError, "in-flight or ambiguous"):
            self.ledger.reopen_terminal_ticket_after_paid_budget(
                self.ticket,
                operator_id="operator",
                reason="paid escalation capacity added",
                now=now + 2,
            )
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], "blocked")
        terminal = self.ledger.connection.execute(
            "SELECT resolved_at FROM terminal_ticket_failures WHERE ticket_id=?", (self.ticket,)
        ).fetchone()
        self.assertIsNone(terminal["resolved_at"])

    def test_migrate_legacy_gateway_generation_suffix_to_explicit_generation(self) -> None:
        with TemporaryDirectory() as td:
            database = Path(td) / "legacy.db"
            legacy = Ledger(database)
            legacy.migrate()
            ticket = legacy.create_ticket(title="legacy-gateway")
            legacy.connection.execute("DROP TABLE gateway_notification_outbox")
            legacy.connection.execute(
                """CREATE TABLE gateway_notification_outbox (
                    operation_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL REFERENCES tickets(id),
                    failure_fingerprint TEXT NOT NULL,
                    target TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at INTEGER,
                    next_attempt_at INTEGER,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    delivered_at INTEGER,
                    terminal_owner TEXT,
                    UNIQUE(ticket_id, failure_fingerprint)
                )"""
            )
            fingerprint = "a" * 64
            legacy.connection.execute(
                "INSERT INTO gateway_notification_outbox(operation_id,ticket_id,failure_fingerprint,target,idempotency_key,payload,status,created_at,updated_at) VALUES ('op-legacy',?,?,?,?,?,'pending',1,1)",
                (ticket, fingerprint + ":g2", "mattermost:ops", "legacy-key", "payload"),
            )
            legacy.migrate()
            row = legacy.connection.execute(
                "SELECT failure_fingerprint,terminal_generation,status,superseded_at FROM gateway_notification_outbox WHERE operation_id='op-legacy'"
            ).fetchone()
            self.assertEqual((row["failure_fingerprint"], row["terminal_generation"], row["status"]), (fingerprint, 2, "pending"))
            self.assertIsNone(row["superseded_at"])
            table_sql = legacy.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='gateway_notification_outbox'"
            ).fetchone()[0]
            self.assertIn("UNIQUE(ticket_id, failure_fingerprint, terminal_generation)", table_sql)
            legacy.close()

    def test_gateway_notification_worker_delivers_once(self) -> None:
        self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="discord:ops",
        )
        calls = []
        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"message_id":"m1"}', "")
        worker = HermesGatewayNotificationWorker(
            self.ledger, executable="hermes", worker_id="notify", runner=runner, clock=lambda: 100
        )
        self.assertEqual(worker.deliver_one().status, "delivered")
        self.assertEqual(worker.deliver_one().status, "no_work")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:4], ("send", "--to", "discord:ops"))
        row = self.ledger.connection.execute("SELECT status FROM gateway_notification_outbox WHERE ticket_id=?", (self.ticket,)).fetchone()
        self.assertEqual(row["status"], "delivered")

    def test_gateway_timeout_becomes_delivery_unknown_and_is_not_retried(self) -> None:
        self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="telegram",
        )
        calls = []
        def runner(argv, **_kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 60)
        worker = HermesGatewayNotificationWorker(
            self.ledger, executable="hermes", worker_id="notify", runner=runner, clock=lambda: 100
        )
        first = worker.deliver_one()
        second = worker.deliver_one()
        self.assertEqual((first.status, second.status), ("delivery_unknown", "no_work"))
        self.assertEqual(len(calls), 1)
        row = self.ledger.connection.execute(
            "SELECT status,next_attempt_at,last_error FROM gateway_notification_outbox WHERE ticket_id=?", (self.ticket,)
        ).fetchone()
        self.assertEqual(row["status"], "delivery_unknown")
        self.assertIsNone(row["next_attempt_at"])
        self.assertIn("may have started", row["last_error"])

    def test_success_response_after_lease_loss_becomes_delivery_unknown(self) -> None:
        self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="telegram",
        )
        def runner(argv, **_kwargs):
            self.ledger.connection.execute(
                "UPDATE gateway_notification_outbox SET lease_expires_at=99 WHERE status='delivering'"
            )
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"message_id":"m-late"}', "")
        worker = HermesGatewayNotificationWorker(
            self.ledger, executable="hermes", worker_id="notify", lease_seconds=60, runner=runner, clock=lambda: 100
        )
        result = worker.deliver_one()
        self.assertEqual(result.status, "delivery_unknown")
        row = self.ledger.connection.execute(
            "SELECT status,last_error FROM gateway_notification_outbox WHERE ticket_id=?", (self.ticket,)
        ).fetchone()
        self.assertEqual(row["status"], "delivery_unknown")
        self.assertIn("returned success", row["last_error"])
        self.assertIsNone(self.ledger.claim_next_gateway_notification("notify-2", lease_seconds=10, now=101))

    def test_expired_delivery_lease_becomes_unknown_and_requires_operator_resolution(self) -> None:
        notice = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="telegram",
        )
        operation_id = notice["operation_id"]
        claimed = self.ledger.claim_next_gateway_notification("notify", lease_seconds=5, now=100)
        self.assertEqual(claimed["operation_id"], operation_id)
        self.assertEqual(self.ledger.recover_expired_gateway_notification_leases(now=106), [operation_id])
        self.assertIsNone(self.ledger.claim_next_gateway_notification("notify-2", lease_seconds=5, now=107))
        status = self.ledger.operator_status()
        self.assertEqual(status["ambiguous_gateway_notifications"][0]["operation_id"], operation_id)
        self.assertEqual(status["ambiguous_gateway_notifications"][0]["ticket_id"], self.ticket)
        with self.assertRaisesRegex(PermissionError, "paused controller"):
            self.ledger.resolve_gateway_notification_unknown(
                operation_id, operator_id="operator", reason="checked gateway history", action="confirm_delivered", now=108
            )
        self.ledger.pause("operator", reason="reconcile ambiguous gateway delivery")
        resolved = self.ledger.resolve_gateway_notification_unknown(
            operation_id, operator_id="operator", reason="gateway history confirms message id", action="confirm_delivered", now=109
        )
        self.assertEqual(resolved["status"], "delivered")
        event = self.ledger.connection.execute(
            "SELECT actor_id,payload_json FROM events WHERE entity_type='gateway_notification' AND entity_id=? ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        self.assertEqual(event["actor_id"], "operator")
        self.assertEqual(json.loads(event["payload_json"])["action"], "confirm_delivered")

    def test_operator_can_explicitly_retry_unknown_delivery_after_pause(self) -> None:
        notice = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="telegram",
        )
        operation_id = notice["operation_id"]
        self.ledger.claim_next_gateway_notification("notify", lease_seconds=5, now=100)
        self.ledger.recover_expired_gateway_notification_leases(now=106)
        self.ledger.pause("operator", reason="choose explicit resend")
        resolved = self.ledger.resolve_gateway_notification_unknown(
            operation_id,
            operator_id="operator",
            reason="gateway audit proves no message was accepted",
            action="retry",
            now=107,
        )
        self.assertEqual(resolved["status"], "retryable")
        self.assertEqual(resolved["next_attempt_at"], 107)
        claimed = self.ledger.claim_next_gateway_notification("notify-2", lease_seconds=5, now=107)
        self.assertEqual(claimed["operation_id"], operation_id)

    def test_permanent_gateway_failure_is_visible_and_requires_explicit_retry(self) -> None:
        notice = self.ledger.record_terminal_unresolvable(
            self.ticket,
            attempt_number=2,
            failure_fingerprint="d" * 64,
            reason="unresolvable",
            summary={"local_feedback": "authoritative findings"},
            notification_target="telegram",
        )
        operation_id = notice["operation_id"]
        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "", "invalid target")
        worker = HermesGatewayNotificationWorker(
            self.ledger, executable="hermes", worker_id="notify", runner=runner, clock=lambda: 100
        )
        result = worker.deliver_one()
        self.assertEqual(result.status, "permanently_failed")
        status = self.ledger.operator_status()
        failed = status["failed_gateway_notifications"]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["operation_id"], failed[0]["ticket_id"], failed[0]["target"]), (operation_id, self.ticket, "telegram"))
        self.assertIn("invalid target", failed[0]["last_error"])
        with self.assertRaisesRegex(PermissionError, "paused controller"):
            self.ledger.resolve_gateway_notification(
                operation_id, operator_id="operator", reason="fixed target", action="retry", now=101
            )
        self.ledger.pause("operator", reason="fix terminal notification transport")
        with self.assertRaisesRegex(ValueError, "may only be retried"):
            self.ledger.resolve_gateway_notification(
                operation_id, operator_id="operator", reason="not actually delivered", action="confirm_delivered", now=102
            )
        resolved = self.ledger.resolve_gateway_notification(
            operation_id, operator_id="operator", reason="gateway target corrected", action="retry", now=103
        )
        self.assertEqual(resolved["status"], "retryable")
        self.assertEqual(resolved["next_attempt_at"], 103)
        event = self.ledger.connection.execute(
            "SELECT event_type,actor_id,payload_json FROM events WHERE entity_type='gateway_notification' AND entity_id=? ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        self.assertEqual((event["event_type"], event["actor_id"]), ("gateway_notification_failure_retried", "operator"))
        self.assertEqual(json.loads(event["payload_json"])["prior_status"], "permanently_failed")

    def test_pending_needs_triage_comment_is_replaced_with_local_feedback(self) -> None:
        ticket = self.ledger.create_ticket(title="feedback", state=CanonicalState.VERIFYING, external_id="H-2")
        self.ledger.transition(ticket, CanonicalState.NEEDS_TRIAGE, payload={"attempt_number": 2, "reason": "deterministic failure"})
        before = self.ledger.connection.execute("SELECT payload FROM evidence_comment_outbox WHERE ticket_id=?", (ticket,)).fetchone()["payload"]
        self.assertNotIn("Actionable repair guidance", before)
        row = self.ledger.replace_pending_triage_comment(
            ticket,
            attempt_number=2,
            evidence="Actionable repair guidance: remove the out-of-scope test file.",
        )
        self.assertIn("Actionable repair guidance", row["payload"])
        self.assertEqual(row["status"], "pending")

    def test_controller_paid_repair_uses_local_feedback_artifact(self) -> None:
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F-1','feature','active',?,?)",
            (now, now),
        )
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F-1' WHERE id=?", (self.ticket,))
        model_calls = []
        class LocalModel:
            provider = "local"
            model = "local-review"
            def invoke(_self, purpose, packet, *, artifact_dir, workdir=None):
                model_calls.append((purpose, packet))
                artifact_dir.mkdir(parents=True, exist_ok=True)
                artifact = artifact_dir / "review-result.json"
                artifact.write_text("{}", encoding="utf-8")
                payload = {
                    "verdict": "repair",
                    "criterion_results": [],
                    "findings": [{
                        "criterion_id": "scope",
                        "severity": "blocking",
                        "file": "scripts/test.mjs",
                        "symbol": "",
                        "evidence": "outside allowlist",
                        "minimal_repair": "remove the out-of-scope edit",
                        "verification": "rerun deterministic validation",
                        "fingerprint_input": "scope",
                    }],
                    "suggestions": ["Keep the repair inside the authorized file set."],
                }
                return SimpleNamespace(payload=payload, artifact_path=artifact)
        paid_packets = []
        class Paid:
            def invoke(_self, feature_id, purpose, request_key, packet):
                paid_packets.append(packet)
                return {"decision": "repair", "feedback": "Use one narrow repair attempt."}
        controller = LocalFirstController(
            self.ledger,
            Board(),
            RuntimeConfig(repository=self.root, worktree_root=self.root / "worktrees", artifact_root=self.root / "artifacts"),
            local_model=LocalModel(),
        )
        result = controller.execute_ticket_paid_escalation(self.ticket, adapter=Paid(), notification_target="telegram")
        self.assertEqual(result["status"], "repair_authorized")
        self.assertEqual(len(model_calls), 1)
        self.assertIn("outside allowlist", paid_packets[0]["local_feedback"])
        feedback_stage = self.ledger.connection.execute(
            "SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage='triage-feedback-2'", (self.ticket,)
        ).fetchone()
        self.assertIsNotNone(feedback_stage)
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.REPAIRING.value)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id=? AND attempt_number=3", (self.ticket,)).fetchone()[0], 1)

    def test_featureless_needs_triage_falls_through_to_legacy_triage(self) -> None:
        self.assertIsNone(self.ledger.ticket_paid_escalation_candidate(ticket_id=self.ticket))
        claim = self.ledger.claim_next_scheduler_triage(
            "triage-worker",
            lease_seconds=30,
            triage_execution_policy_hash="triage-policy",
            now=100,
            ticket_id=self.ticket,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim["ticket_id"], self.ticket)

    def test_ticket_paid_escalation_precedes_legacy_triage(self) -> None:
        calls = []
        now = self.ledger._now()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F-paid','feature','active',?,?)",
            (now, now),
        )
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F-paid' WHERE id=?", (self.ticket,))
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="scheduler",
            lease_seconds=30,
            ticket_escalation_runner=lambda ticket_id: calls.append(ticket_id) or {"status": "repair_authorized"},
            triage_runner=lambda _ticket_id: (_ for _ in ()).throw(AssertionError("legacy triage should not run")),
            triage_execution_policy_hash="triage-policy",
            clock=lambda: 100,
        )
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.ticket_id, result.status), ("ticket_paid_escalation", self.ticket, "repair_authorized"))
        self.assertEqual(calls, [self.ticket])


if __name__ == "__main__":
    unittest.main()
