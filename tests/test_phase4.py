from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.triage import LocalTriagePlanner, TriageCoordinator, TriageError, normalize_triage


class Phase4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.parent_contract = self.contract("PARENT", ("AC-1", "AC-2"))
        self.parent_id = self.ledger.create_ticket(
            title="parent", state=CanonicalState.NEEDS_TRIAGE, contract=self.parent_contract.contract()
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.tempdir.cleanup()

    @staticmethod
    def contract(ticket_id: str, criteria: tuple[str, ...] = ("AC-1",)) -> MicroTicket:
        return MicroTicket(
            ticket_id=ticket_id,
            objective="Reject only negative quantities safely.",
            criterion_ids=criteria,
            primary_symbol="app.py::classify",
            allowed_files=("app.py", "test_app.py"),
            forbidden_changes=("Do not alter public API.",),
            patch_budget=PatchBudget(),
            verification=VerificationProfile(commands=(("python", "-m", "unittest"),)),
            risk="low",
            review_required=True,
            max_attempts=2,
            dependencies=(),
        )

    def valid_payload(self, *children: dict[str, object]) -> dict[str, object]:
        return {
            "classification": "oversized_ticket",
            "root_cause_evidence": "Two independent guards share one broad ticket.",
            "recommended_action": "decompose",
            "children": list(children),
        }

    def child(self, ticket_id: str = "CHILD-1", criteria: tuple[str, ...] = ("AC-1",)) -> dict[str, object]:
        return {"id": ticket_id, "resolves_criteria": list(criteria), "ticket": self.contract(ticket_id, criteria).contract()}

    def test_local_triage_planner_packet_exposes_closed_v2_contract(self) -> None:
        planner = LocalTriagePlanner(provider="provider", model="model", timeout_seconds=23)
        payload = json.loads(
            planner.packet(
                self.parent_contract,
                parent_depth=1,
                unresolved_criteria={"AC-2", "AC-1"},
                failure_evidence="validation failed twice",
            )
        )
        contract = payload["output_contract"]
        self.assertFalse(contract["additionalProperties"])
        self.assertEqual(
            contract["required"],
            ["classification", "root_cause_evidence", "recommended_action", "children"],
        )
        self.assertEqual(contract["children"]["maxItems"], 3)
        self.assertFalse(contract["children"]["items"]["additionalProperties"])
        self.assertIn("ticket", contract["children"]["items"]["required"])
        self.assertEqual(payload["unresolved_criteria"], ["AC-1", "AC-2"])
        self.assertEqual(
            planner.execution_policy_hash(),
            LocalTriagePlanner(provider="provider", model="model", timeout_seconds=23).execution_policy_hash(),
        )

    def test_classification_first_rejects_environment_and_invalid_action(self) -> None:
        with self.assertRaisesRegex(TriageError, "must not create children"):
            normalize_triage({
                "classification": "environment_failure",
                "root_cause_evidence": "Compiler unavailable in fixture.",
                "recommended_action": "decompose",
                "children": [self.child()],
            }, self.parent_contract, parent_depth=0)
        with self.assertRaisesRegex(TriageError, "must not create children"):
            normalize_triage({"classification":"architecture_gap", "root_cause_evidence":"design unknown", "recommended_action":"decompose", "children":[self.child()]}, self.parent_contract, parent_depth=0)
        with self.assertRaisesRegex(TriageError, "recommended action"):
            normalize_triage({
                "classification": "oversized_ticket",
                "root_cause_evidence": "Ticket needs splitting.",
                "recommended_action": "repair",
                "children": [],
            }, self.parent_contract, parent_depth=0)

    def test_rejects_scope_expansion_depth_count_unresolved_and_duplicate_children(self) -> None:
        with self.assertRaisesRegex(TriageError, "unresolved"):
            normalize_triage(self.valid_payload(self.child(criteria=("AC-9",))), self.parent_contract, parent_depth=0)
        with self.assertRaisesRegex(TriageError, "depth"):
            normalize_triage(self.valid_payload(self.child()), self.parent_contract, parent_depth=2)
        with self.assertRaisesRegex(TriageError, "maximum"):
            normalize_triage(self.valid_payload(self.child("C1"), self.child("C2"), self.child("C3"), self.child("C4")), self.parent_contract, parent_depth=0)
        duplicate = self.child("C1")
        with self.assertRaisesRegex(TriageError, "duplicate"):
            normalize_triage(self.valid_payload(duplicate, duplicate), self.parent_contract, parent_depth=0)

    def test_triage_creates_bounded_children_pauses_parent_and_resumes_only_after_all_accepted(self) -> None:
        coordinator = TriageCoordinator(self.ledger)
        result = normalize_triage(
            self.valid_payload(self.child("C1", ("AC-1",)), self.child("C2", ("AC-2",))),
            self.parent_contract,
            parent_depth=0,
        )
        child_ids = coordinator.apply(self.parent_id, result)
        self.assertEqual(len(child_ids), 2)
        self.assertTrue(self.ledger.parent_is_paused(self.parent_id))
        self.assertEqual(self.ledger.get_ticket(self.parent_id)["state"], CanonicalState.NEEDS_TRIAGE.value)
        self.assertEqual({self.ledger.get_ticket(child_id)["parent_ticket_id"] for child_id in child_ids}, {self.parent_id})
        self.assertEqual({self.ledger.get_ticket(child_id)["depth"] for child_id in child_ids}, {1})
        parent_events = self.ledger.events_for(self.parent_id)
        triage_event = next(event for event in parent_events if event["event_type"] == "triage_children_created")
        self.assertIn('"classification": "oversized_ticket"', triage_event["payload_json"])
        self.assertIn('"root_cause_evidence": "Two independent guards share one broad ticket."', triage_event["payload_json"])
        self.assertFalse(coordinator.resolve_parent(self.parent_id))

        for child_id in child_ids:
            self.ledger.transition(child_id, CanonicalState.IMPLEMENTING)
            self.ledger.transition(child_id, CanonicalState.VERIFYING)
            self.ledger.transition(child_id, CanonicalState.ACCEPTED)
        self.assertTrue(coordinator.resolve_parent(self.parent_id))
        self.assertFalse(self.ledger.parent_is_paused(self.parent_id))
        self.assertEqual(self.ledger.get_ticket(self.parent_id)["state"], CanonicalState.READY_LOCAL.value)

    def test_child_rejection_blocks_parent_and_reapplication_is_idempotent(self) -> None:
        coordinator = TriageCoordinator(self.ledger)
        self.ledger.set_criterion_status(self.parent_id, "AC-1", "accepted", evidence="previous acceptance")
        stale_result = normalize_triage(self.valid_payload(self.child()), self.parent_contract, parent_depth=0)
        with self.assertRaisesRegex(TriageError, "unresolved"):
            coordinator.apply(self.parent_id, stale_result)
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.ledger.create_triaged_children(
                self.parent_id,
                [("unsafe", MicroTicket(
                    ticket_id="UNSAFE", objective="Reject only negative quantities safely.", criterion_ids=("AC-1",),
                    primary_symbol="app.py::classify", allowed_files=("app.py", "test_app.py", "extra.py"),
                    forbidden_changes=("Do not alter public API.",), patch_budget=PatchBudget(max_files=3),
                    verification=VerificationProfile(commands=(("python", "-m", "unittest"),)),
                    risk="low", review_required=True, max_attempts=99, dependencies=(),
                ), self.ledger._triage_child_fingerprint(MicroTicket(
                    ticket_id="UNSAFE", objective="Reject only negative quantities safely.", criterion_ids=("AC-1",),
                    primary_symbol="app.py::classify", allowed_files=("app.py", "test_app.py", "extra.py"),
                    forbidden_changes=("Do not alter public API.",), patch_budget=PatchBudget(max_files=3),
                    verification=VerificationProfile(commands=(("python", "-m", "unittest"),)),
                    risk="low", review_required=True, max_attempts=99, dependencies=(),
                )))],
                classification="oversized_ticket",
                root_cause_evidence="Unsafe direct child must still be rejected.",
            )
        self.ledger.set_criterion_status(self.parent_id, "AC-1", "open", evidence="invalidated by integration")
        result = normalize_triage(self.valid_payload(self.child()), self.parent_contract, parent_depth=0)
        child_id = coordinator.apply(self.parent_id, result)[0]
        self.assertEqual(coordinator.apply(self.parent_id, result), [child_id])
        self.ledger.close()
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        coordinator = TriageCoordinator(self.ledger)
        self.assertEqual(coordinator.apply(self.parent_id, result), [child_id])
        different = normalize_triage(self.valid_payload(self.child("C2", ("AC-2",))), self.parent_contract, parent_depth=0)
        with self.assertRaisesRegex(ValueError, "different children"):
            coordinator.apply(self.parent_id, different)
        self.ledger.transition(child_id, CanonicalState.BLOCKED)
        self.ledger.transition(child_id, CanonicalState.REJECTED)
        self.assertTrue(coordinator.resolve_parent(self.parent_id))
        self.assertEqual(self.ledger.get_ticket(self.parent_id)["state"], CanonicalState.BLOCKED.value)


if __name__ == "__main__":
    unittest.main()
