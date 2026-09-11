from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.architecture import ArchitectureError, import_architecture_packet
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import HermesPaidModelAdapter, InjectedPaidModelAdapter, PaidInvocationError
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


if __name__ == "__main__":
    unittest.main()
