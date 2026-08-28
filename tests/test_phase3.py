from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.local_qwen import LocalQwenAdapter
from local_first_orchestrator.review import (
    LocalReviewAdapter,
    ReviewPacketBuilder,
    SameTicketRepairCoordinator,
    failure_fingerprint,
    normalize_review,
)
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class Phase3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = MicroTicket(
            ticket_id="T-3", objective="Return ok.", criterion_ids=("AC-1",),
            primary_symbol="app.py::classify", allowed_files=("app.py", "test_app.py"),
            forbidden_changes=(), patch_budget=PatchBudget(),
            verification=VerificationProfile(commands=(("python", "-m", "unittest"),)),
            risk="low", review_required=True, max_attempts=2, dependencies=(),
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.tempdir.cleanup()

    def local_review_ticket(self) -> str:
        ticket_id = self.ledger.create_ticket(title="review fixture", contract=self.ticket.contract())
        for state in (CanonicalState.READY_LOCAL, CanonicalState.IMPLEMENTING, CanonicalState.VERIFYING, CanonicalState.LOCAL_REVIEW):
            self.ledger.transition(ticket_id, state)
        return ticket_id

    def repair_payload(self, *, evidence: str = "line 11 at 2026-01-01T10:00:00Z: wrong guard") -> dict[str, object]:
        return {"verdict": "repair", "criterion_results": [{"criterion_id": "AC-1", "status": "fail", "evidence": evidence}], "findings": [{"criterion_id": "AC-1", "severity": "blocking", "file": "app.py", "symbol": "classify", "evidence": evidence, "minimal_repair": "Reject negative values.", "verification": "python -m unittest", "fingerprint_input": "negative guard"}], "suggestions": []}

    def test_strict_pass_closes_criterion_and_accepts_ticket(self) -> None:
        ticket_id = self.local_review_ticket()
        result = normalize_review({"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "test passed"}], "findings": [], "suggestions": []}, self.ticket)
        outcome = SameTicketRepairCoordinator(self.ledger).apply(ticket_id, 1, result)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(self.ledger.get_ticket(ticket_id)["state"], "accepted")
        self.assertEqual(self.ledger.criterion_status(ticket_id, "AC-1"), "accepted")

    def test_valid_repair_stays_on_same_ticket_then_passes(self) -> None:
        ticket_id = self.local_review_ticket()
        coordinator = SameTicketRepairCoordinator(self.ledger)
        self.assertEqual(coordinator.apply(ticket_id, 1, normalize_review(self.repair_payload(), self.ticket)), "repair")
        self.assertEqual(self.ledger.get_ticket(ticket_id)["state"], "repairing")
        self.assertEqual(self.ledger.attempt_count(ticket_id), 1)
        self.ledger.transition(ticket_id, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket_id, CanonicalState.VERIFYING)
        self.ledger.transition(ticket_id, CanonicalState.LOCAL_REVIEW)
        passed = normalize_review({"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "fixed"}], "findings": [], "suggestions": []}, self.ticket)
        self.assertEqual(coordinator.apply(ticket_id, 2, passed), "accepted")
        self.assertEqual(self.ledger.get_ticket(ticket_id)["state"], "accepted")
        self.assertEqual(self.ledger.attempt_count(ticket_id), 2)

    def test_pass_requires_every_ticket_criterion_to_have_pass_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "every criterion"):
            normalize_review({"verdict": "pass", "criterion_results": [], "findings": [], "suggestions": []}, self.ticket)

    def test_malformed_or_out_of_scope_blockers_become_suggestions(self) -> None:
        result = normalize_review({"verdict": "repair", "criterion_results": [], "findings": [{"criterion_id": "OTHER", "severity": "blocking", "file": "outside.py", "symbol": "x"}], "suggestions": []}, self.ticket)
        self.assertEqual(result.findings, ())
        self.assertEqual(len(result.suggestions), 1)

    def test_suggestions_do_not_consume_attempt(self) -> None:
        ticket_id = self.local_review_ticket()
        result = normalize_review({"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "ok"}], "findings": [], "suggestions": ["rename helper"]}, self.ticket)
        SameTicketRepairCoordinator(self.ledger).apply(ticket_id, 1, result)
        self.assertEqual(self.ledger.attempt_count(ticket_id), 0)

    def test_fingerprint_ignores_line_numbers_timestamps_and_absolute_paths(self) -> None:
        left = failure_fingerprint("T-3", "review", "AC-1", "app.py", "classify", "line 11 at 2026-01-01T10:00:00Z /tmp/a: wrong guard")
        right = failure_fingerprint("T-3", "review", "AC-1", "app.py", "classify", "line 99 at 2027-02-02T11:11:11Z /other/b: wrong guard")
        self.assertEqual(left, right)

    def test_repeated_identical_failure_routes_once_to_triage(self) -> None:
        ticket_id = self.local_review_ticket()
        coordinator = SameTicketRepairCoordinator(self.ledger)
        self.assertEqual(coordinator.apply(ticket_id, 1, normalize_review(self.repair_payload(), self.ticket)), "repair")
        self.ledger.transition(ticket_id, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket_id, CanonicalState.VERIFYING)
        self.ledger.transition(ticket_id, CanonicalState.LOCAL_REVIEW)
        self.assertEqual(coordinator.apply(ticket_id, 2, normalize_review(self.repair_payload(evidence="line 88 at 2028-03-03T12:12:12Z: wrong guard"), self.ticket)), "triage")
        self.assertEqual(coordinator.apply(ticket_id, 2, normalize_review(self.repair_payload(), self.ticket)), "triage")
        transitions = [e for e in self.ledger.events_for(ticket_id) if e["to_state"] == "needs_triage"]
        self.assertEqual(len(transitions), 1)

    def test_max_attempts_routes_to_triage_even_for_new_fingerprint(self) -> None:
        ticket_id = self.local_review_ticket()
        coordinator = SameTicketRepairCoordinator(self.ledger)
        coordinator.apply(ticket_id, 1, normalize_review(self.repair_payload(), self.ticket))
        self.ledger.transition(ticket_id, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket_id, CanonicalState.VERIFYING)
        self.ledger.transition(ticket_id, CanonicalState.LOCAL_REVIEW)
        payload = self.repair_payload(evidence="other bug")
        payload["findings"][0]["fingerprint_input"] = "other bug"  # type: ignore[index]
        self.assertEqual(coordinator.apply(ticket_id, 2, normalize_review(payload, self.ticket)), "triage")

    def test_fresh_review_packet_excludes_implementation_history_and_fake_runner_only(self) -> None:
        packet = ReviewPacketBuilder().build(self.ticket, diff="diff", selected_files={"app.py": "def classify(): pass"}, validation_evidence="tests pass")
        self.assertNotIn("implementation reasoning", packet)
        self.assertNotIn("arbitrary repository history", packet)
        calls: list[tuple[str, ...]] = []
        def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "ok"}], "findings": [], "suggestions": []}), stderr="")
        review = LocalReviewAdapter(LocalQwenAdapter(runner=runner)).review(self.ticket, packet, artifact_dir=self.root / "artifacts")
        self.assertEqual(review.verdict, "pass")
        self.assertEqual(calls[0][calls[0].index("--query") + 1], packet)
        self.assertNotIn("board", " ".join(calls[0]).lower())


if __name__ == "__main__":
    unittest.main()
