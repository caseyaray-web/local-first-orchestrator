from __future__ import annotations

import threading
import subprocess
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.architecture import ArchitectureError, import_architecture_packet
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import HermesPaidModelAdapter, InjectedPaidModelAdapter, PaidInvocationError, PaidProviderRejectedError
from local_first_orchestrator.usage_governor import PaidPurpose, UsageGovernor


def ticket(ticket_id: str, criterion: str = "AC-1") -> dict[str, object]:
    return {
        "id": ticket_id,
        "objective": "Reject a negative quantity at the bounded calculation boundary.",
        "acceptance_criteria": [criterion],
        "primary_symbol": "app.py::calculate",
        "allowed_files": ["app.py", "test_app.py"],
        "forbidden_changes": ["Do not change the public API."],
        "patch_budget": {"max_files": 2, "max_changed_lines": 180},
        "verification_commands": [["python", "-m", "unittest"]],
        "risk": "low",
        "review_required": True,
        "max_attempts": 2,
        "dependencies": [],
    }


def packet() -> dict[str, object]:
    return {
        "version": 1,
        "feature": {"id": "F-1", "title": "Quantity safety", "objective": "Reject negative quantity input.", "non_goals": ["Do not change public APIs."], "invariants": ["Positive quantity behavior is unchanged."]},
        "acceptance_criteria": [{"id": "AC-1", "statement": "Negative quantities are rejected.", "verification": "unit test"}],
        "decisions": [{"id": "D-1", "decision": "Validate at calculation boundary.", "rationale": "Single bounded behavior.", "alternatives_rejected": ["Validate in callers: duplicates logic."]}],
        "risks": [{"category": "input", "handling": "local_validation"}],
        "tranches": [{"id": "T-1", "objective": "Implement the input guard.", "base_sha": "a" * 40, "integration_verification": [["python", "-m", "unittest"]], "microtickets": [ticket("TK-1")]}, {"id": "T-2", "objective": "Prove the guard remains integrated.", "base_sha": "a" * 40, "integration_verification": [["python", "-m", "unittest"]], "microtickets": [ticket("TK-2")] }],
    }


class Phase5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.ledger = Ledger(Path(self.tempdir.name) / "ledger.db")
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.tempdir.cleanup()

    def test_atomic_reservation_idempotency_completion_release_and_unknown_no_retry(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=1, checkpoint=0, escalation=0)
        results: list[str] = []
        def reserve() -> None:
            try:
                results.append(governor.authorize("F", PaidPurpose.ARCHITECTURE, "same").reservation_id)
            except PermissionError:
                results.append("denied")
        threads = [threading.Thread(target=reserve) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(set(results)), 1)
        reservation = governor.authorize("F", PaidPurpose.ARCHITECTURE, "same")
        governor.complete(reservation, input_tokens=2, output_tokens=3)
        self.assertEqual(governor.usage("F", PaidPurpose.ARCHITECTURE)["completed"], 1)
        with self.assertRaises(PermissionError): governor.authorize("F", PaidPurpose.ARCHITECTURE, "another")
        governor.configure("G", architecture=1, checkpoint=0, escalation=0)
        released = governor.authorize("G", PaidPurpose.ARCHITECTURE, "release")
        governor.release(released, "runner did not start")
        self.assertEqual(governor.usage("G", PaidPurpose.ARCHITECTURE)["released"], 1)
        unknown = governor.authorize("G", PaidPurpose.ARCHITECTURE, "unknown")
        governor.unknown(unknown, "transport ambiguous")
        with self.assertRaisesRegex(PermissionError, "unknown"):
            governor.authorize("G", PaidPurpose.ARCHITECTURE, "unknown")

    def test_exhaustion_prevents_provider_call_and_approval_is_bounded_to_purpose(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=0)
        called = []
        adapter = InjectedPaidModelAdapter(self.ledger, governor, lambda packet: called.append(packet) or {"proposal": "ok"})
        with self.assertRaises(PaidInvocationError): adapter.invoke("F", PaidPurpose.ARCHITECTURE, "r1", {"x": 1})
        self.assertEqual(called, [])
        approval = governor.approve("F", PaidPurpose.ARCHITECTURE, "operator", "one architecture call", "approval-1")
        self.assertEqual(approval.calls, 1)
        self.assertEqual(governor.approve("F", PaidPurpose.ARCHITECTURE, "operator", "one architecture call", "approval-1"), approval)
        adapter.invoke("F", PaidPurpose.ARCHITECTURE, "r2", {"x": 2})
        self.assertEqual(len(called), 1)
        with self.assertRaises(PermissionError): governor.authorize("F", PaidPurpose.ESCALATION, "e1")

    def test_packet_rejection_bounded_activation_and_explicit_snapshot_revalidation(self) -> None:
        invalid = packet(); invalid["tranches"][0]["microtickets"][0]["acceptance_criteria"] = ["MISSING"]  # type: ignore[index]
        with self.assertRaises(ArchitectureError): import_architecture_packet(self.ledger, invalid, max_active_tickets=1, current_snapshot="a" * 40)
        result = import_architecture_packet(self.ledger, packet(), max_active_tickets=1, current_snapshot="a" * 40)
        self.assertEqual(result.activated_ticket_ids, ("TK-1",))
        self.assertEqual(self.ledger.get_ticket("TK-1")["state"], "ready_local")
        self.assertEqual(self.ledger.get_ticket("TK-2")["state"], "draft")
        with self.assertRaisesRegex(ArchitectureError, "snapshot"):
            result.activate_next(self.ledger, current_snapshot="b" * 40)
        self.assertEqual(result.activate_next(self.ledger, current_snapshot="a" * 40), ("TK-2",))

    def test_checkpoint_packet_only_contains_accepted_tranche_evidence(self) -> None:
        result = import_architecture_packet(self.ledger, packet(), max_active_tickets=2, current_snapshot="a" * 40)
        self.ledger.record_accepted_evidence("TK-1", "c" * 40, "small diff", "tests pass", local_reasoning="forbidden")
        checkpoint = result.checkpoint_packet(self.ledger, "T-1")
        self.assertIn("accepted_commits", checkpoint)
        self.assertEqual(checkpoint["invariants"], ["Positive quantity behavior is unchanged."])
        self.assertEqual(checkpoint["decisions"][0]["id"], "D-1")
        self.assertNotIn("local_reasoning", repr(checkpoint))
        self.assertNotIn("unrelated", repr(checkpoint))

    def test_in_flight_paid_call_from_a_crash_is_marked_unknown_and_never_reinvoked(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=1, checkpoint=0, escalation=0)
        reservation = governor.authorize("F", PaidPurpose.ARCHITECTURE, "interrupted")
        with self.ledger._transaction() as conn:
            now = self.ledger._now()
            conn.execute(
                "INSERT INTO model_calls(id, feature_id, purpose, reservation_id, status, request_artifact_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'in_flight', ?, ?, ?)",
                ("crashed-call", "F", PaidPurpose.ARCHITECTURE.value, reservation.reservation_id, "{}", now, now),
            )
        calls = []
        adapter = InjectedPaidModelAdapter(self.ledger, governor, lambda payload: calls.append(payload) or {"proposal": "wrong"})
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            adapter.invoke("F", PaidPurpose.ARCHITECTURE, "interrupted", {"request": 1})
        self.assertEqual(calls, [])
        self.assertEqual(governor.usage("F", PaidPurpose.ARCHITECTURE)["unknown_outcome"], 1)

    def test_hermes_paid_adapter_uses_safe_explicit_provider_model_route(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("P", (), {"returncode": 0, "stdout": '{"decision":"approve","rationale":"ok"}', "stderr": ""})()
        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
        )
        result = adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "request", {"x": 1})
        self.assertEqual(result["decision"], "approve")
        argv, kwargs = calls[0]
        self.assertEqual(argv[:4], ("hermes", "chat", "--toolsets", "safe"))
        self.assertIn(("--provider", "paid-provider"), tuple(zip(argv, argv[1:])))
        self.assertIn(("--model", "paid-model"), tuple(zip(argv, argv[1:])))
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["completed"], 1)

    def test_confirmed_rate_limit_retries_same_reservation_without_consuming_budget_and_alerts_once(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)
        ticket_id = self.ledger.create_ticket(title="provider-alert")
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if len(calls) <= 3:
                return type("P", (), {"returncode": 1, "stdout": "", "stderr": "429 Too Many Requests: rate limit exceeded api_key=super-secret"})()
            return type("P", (), {"returncode": 0, "stdout": '{"decision":"repair","feedback":"ok"}', "stderr": ""})()
        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
            provider_alert_target="mattermost:ops",
            provider_alert_failure_threshold=3,
        )
        request_key = "ticket-escalation:test"
        reservation_id = None
        for expected_count in (1, 2, 3):
            with self.assertRaises(PaidProviderRejectedError) as raised:
                adapter.invoke("F", PaidPurpose.ESCALATION, request_key, {"ticket_id": ticket_id})
            self.assertEqual(raised.exception.kind, "rate_limited")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(raised.exception.failure_count, expected_count)
            row = self.ledger.connection.execute("SELECT * FROM paid_reservations WHERE feature_id='F' AND purpose='escalation' AND request_key=?", (request_key,)).fetchone()
            reservation_id = str(row["id"])
            self.assertEqual(row["status"], "provider_rejected")
            self.assertEqual(int(row["provider_failure_count"]), expected_count)
            self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM paid_reservations WHERE feature_id='F' AND purpose='escalation' AND status IN ('in_flight','completed','unknown_outcome')").fetchone()[0], 0)
            self.ledger.connection.execute("UPDATE paid_reservations SET provider_retry_after_at=0 WHERE id=?", (reservation_id,))
        alert = self.ledger.connection.execute("SELECT * FROM gateway_notification_outbox WHERE ticket_id=? AND terminal_generation=0", (ticket_id,)).fetchone()
        self.assertIsNotNone(alert)
        self.assertEqual((alert["target"], alert["status"]), ("mattermost:ops", "pending"))
        self.assertIn("Confirmed rejections: 3", alert["payload"])
        self.assertIn("api_key=[REDACTED]", alert["payload"])
        self.assertNotIn("super-secret", alert["payload"])
        self.assertIsNotNone(self.ledger.connection.execute("SELECT provider_alerted_at FROM paid_reservations WHERE id=?", (reservation_id,)).fetchone()[0])
        result = adapter.invoke("F", PaidPurpose.ESCALATION, request_key, {"ticket_id": ticket_id})
        self.assertEqual(result["decision"], "repair")
        self.assertEqual(governor.usage("F", PaidPurpose.ESCALATION)["completed"], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM gateway_notification_outbox WHERE ticket_id=? AND terminal_generation=0", (ticket_id,)).fetchone()[0], 1)
        model_call = self.ledger.connection.execute("SELECT * FROM model_calls WHERE reservation_id=?", (reservation_id,)).fetchone()
        self.assertEqual((model_call["status"], model_call["provider_attempt_count"]), ("completed", 4))

    def test_quota_rejection_is_budget_neutral_and_uses_longer_backoff(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        def runner(argv, **kwargs):
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "insufficient_quota: usage limit exceeded"})()
        adapter = HermesPaidModelAdapter(
            self.ledger, governor, executable="hermes", provider="paid-provider", model="paid-model", profile="paid-profile", runner=runner
        )
        before = self.ledger._now()
        with self.assertRaises(PaidProviderRejectedError) as raised:
            adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "quota", {"x": 1})
        self.assertEqual(raised.exception.kind, "quota_exhausted")
        self.assertGreaterEqual(int(raised.exception.retry_after_at or 0), before + 900)
        row = self.ledger.connection.execute("SELECT status FROM paid_reservations WHERE feature_id='F' AND request_key='quota'").fetchone()
        self.assertEqual(row["status"], "provider_rejected")
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["completed"], 0)
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["unknown_outcome"], 0)

    def test_503_provider_failure_is_ambiguous_and_never_auto_retried(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "503 Service Unavailable"})()

        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
        )
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "provider-503", {"x": 1})
        reservation = self.ledger.connection.execute(
            "SELECT status FROM paid_reservations WHERE feature_id='F' AND request_key='provider-503'"
        ).fetchone()
        model_call = self.ledger.connection.execute(
            "SELECT status FROM model_calls WHERE reservation_id=(SELECT id FROM paid_reservations WHERE feature_id='F' AND request_key='provider-503')"
        ).fetchone()
        self.assertEqual((reservation["status"], model_call["status"]), ("unknown_outcome", "unknown_outcome"))
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "provider-503", {"x": 1})
        self.assertEqual(len(calls), 1)

    def test_confirmed_provider_rejection_updates_reservation_and_model_call_together(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)

        def runner(argv, **kwargs):
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "429 Too Many Requests"})()

        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
        )
        with self.assertRaises(PaidProviderRejectedError):
            adapter.invoke("F", PaidPurpose.ESCALATION, "atomic-429", {"x": 1})
        row = self.ledger.connection.execute(
            "SELECT r.status AS reservation_status,m.status AS model_status,r.provider_error_kind,m.last_provider_error_kind "
            "FROM paid_reservations r JOIN model_calls m ON m.reservation_id=r.id "
            "WHERE r.feature_id='F' AND r.request_key='atomic-429'"
        ).fetchone()
        self.assertEqual(
            (row["reservation_status"], row["model_status"], row["provider_error_kind"], row["last_provider_error_kind"]),
            ("provider_rejected", "provider_rejected", "rate_limited", "rate_limited"),
        )

    def test_confirmed_provider_rejection_rolls_back_both_rows_if_model_call_update_fails(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)
        reservation = governor.authorize("F", PaidPurpose.ESCALATION, "atomic-rollback")
        call_id = uuid.uuid4().hex
        self.ledger.connection.execute(
            "INSERT INTO model_calls(id,feature_id,purpose,reservation_id,status,request_artifact_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (call_id, "F", "escalation", reservation.reservation_id, "in_flight", "{}", 1, 1),
        )
        self.ledger.connection.execute(
            "CREATE TRIGGER fail_provider_rejected_model_update BEFORE UPDATE OF status ON model_calls "
            "WHEN NEW.status='provider_rejected' BEGIN SELECT RAISE(ABORT, 'forced model update failure'); END"
        )
        with self.assertRaisesRegex(Exception, "forced model update failure"):
            governor.provider_rejected(
                reservation,
                call_id,
                kind="rate_limited",
                detail="429",
                retryable=True,
                retry_after_at=0,
            )
        row = self.ledger.connection.execute(
            "SELECT r.status AS reservation_status,m.status AS model_status,r.provider_failure_count "
            "FROM paid_reservations r JOIN model_calls m ON m.reservation_id=r.id WHERE r.id=?",
            (reservation.reservation_id,),
        ).fetchone()
        self.assertEqual((row["reservation_status"], row["model_status"], row["provider_failure_count"]), ("in_flight", "in_flight", 0))

    def test_provider_retry_rechecks_budget_after_another_request_spends_the_freed_slot(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)
        first = governor.authorize("F", PaidPurpose.ESCALATION, "first")
        model_call_id = uuid.uuid4().hex
        self.ledger.connection.execute(
            "INSERT INTO model_calls(id,feature_id,purpose,reservation_id,status,request_artifact_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (model_call_id, "F", "escalation", first.reservation_id, "in_flight", "{}", 1, 1),
        )
        governor.provider_rejected(first, model_call_id, kind="rate_limited", detail="429", retryable=True, retry_after_at=0)
        second = governor.authorize("F", PaidPurpose.ESCALATION, "second")
        governor.complete(second, input_tokens=0, output_tokens=0)
        with self.assertRaisesRegex(PermissionError, "budget exhausted while provider retry was waiting"):
            governor.authorize("F", PaidPurpose.ESCALATION, "first")
        row = self.ledger.connection.execute("SELECT status FROM paid_reservations WHERE id=?", (first.reservation_id,)).fetchone()
        self.assertEqual(row["status"], "provider_rejected")

    def test_auth_rejection_is_nonretryable_and_alerts_immediately(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)
        ticket_id = self.ledger.create_ticket(title="auth-alert")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "401 Unauthorized api_key=super-secret"})()

        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
            provider_alert_target="mattermost:ops",
            provider_alert_failure_threshold=3,
        )
        with self.assertRaises(PaidProviderRejectedError) as raised:
            adapter.invoke("F", PaidPurpose.ESCALATION, "auth", {"ticket_id": ticket_id})
        self.assertEqual(raised.exception.kind, "authentication_rejected")
        self.assertFalse(raised.exception.retryable)
        self.assertIsNone(raised.exception.retry_after_at)
        reservation = self.ledger.connection.execute(
            "SELECT * FROM paid_reservations WHERE feature_id='F' AND request_key='auth'"
        ).fetchone()
        self.assertEqual((reservation["status"], reservation["provider_retryable"]), ("provider_rejected", 0))
        alert = self.ledger.connection.execute(
            "SELECT * FROM gateway_notification_outbox WHERE ticket_id=? AND terminal_generation=0", (ticket_id,)
        ).fetchone()
        self.assertIsNotNone(alert)
        self.assertIn("authentication_rejected", alert["payload"])
        self.assertNotIn("super-secret", alert["payload"])
        with self.assertRaises(PaidInvocationError):
            adapter.invoke("F", PaidPurpose.ESCALATION, "auth", {"ticket_id": ticket_id})
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.ledger.connection.execute("SELECT COUNT(*) FROM gateway_notification_outbox WHERE ticket_id=? AND terminal_generation=0", (ticket_id,)).fetchone()[0],
            1,
        )

    def test_provider_rejection_and_required_alert_roll_back_together(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=1)
        ticket_id = self.ledger.create_ticket(title="atomic-auth-alert")
        self.ledger.connection.execute(
            "CREATE TRIGGER fail_provider_incident_alert BEFORE INSERT ON gateway_notification_outbox WHEN NEW.terminal_generation=0 BEGIN SELECT RAISE(ABORT, 'forced alert failure'); END"
        )

        def runner(argv, **kwargs):
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "401 Unauthorized"})()

        adapter = HermesPaidModelAdapter(
            self.ledger,
            governor,
            executable="hermes",
            provider="paid-provider",
            model="paid-model",
            profile="paid-profile",
            runner=runner,
            provider_alert_target="mattermost:ops",
        )
        with self.assertRaisesRegex(Exception, "forced alert failure"):
            adapter.invoke("F", PaidPurpose.ESCALATION, "atomic-auth-alert", {"ticket_id": ticket_id})
        row = self.ledger.connection.execute(
            "SELECT r.status AS reservation_status,m.status AS model_status,r.provider_alerted_at "
            "FROM paid_reservations r JOIN model_calls m ON m.reservation_id=r.id "
            "WHERE r.feature_id='F' AND r.request_key='atomic-auth-alert'"
        ).fetchone()
        self.assertEqual((row["reservation_status"], row["model_status"]), ("in_flight", "in_flight"))
        self.assertIsNone(row["provider_alerted_at"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM gateway_notification_outbox WHERE ticket_id=?", (ticket_id,)).fetchone()[0], 0)

    def test_timeout_remains_unknown_outcome_and_is_never_auto_retried(self) -> None:
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 30)
        adapter = HermesPaidModelAdapter(
            self.ledger, governor, executable="hermes", provider="paid-provider", model="paid-model", profile="paid-profile", runner=runner
        )
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "timeout", {"x": 1})
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["unknown_outcome"], 1)
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            adapter.invoke("F", PaidPurpose.INTEGRATION_CHECKPOINT, "timeout", {"x": 1})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
