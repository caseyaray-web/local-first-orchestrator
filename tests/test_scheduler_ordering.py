from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import (
    ProcessNextScheduler,
    SCHEDULER_STAGE_ORDER,
    preview_next,
    scheduler_stage_rank,
)
from local_first_orchestrator.states import CanonicalState


class Board:
    timeout_seconds = 1
    def set_state(self, *args, **kwargs): return None
    def find_comment_marker(self, *args, **kwargs): return "not_found"
    def deliver_comment(self, *args, **kwargs): return None
    def create_microticket(self, *args, **kwargs): return "external-generated"


class ImplementationChosen(RuntimeError):
    pass


class ValidationChosen(RuntimeError):
    pass


class SchedulerOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "ledger.db"
        self.ledger = Ledger(self.database)
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    @staticmethod
    def contract() -> dict[str, object]:
        return {
            "objective": "exercise deterministic scheduler ordering",
            "criterion_ids": ["AC-1"],
            "primary_symbol": "app.py::value",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["no unrelated changes"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-c", "pass"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 1,
            "dependencies": [],
        }

    def ticket(self, title: str, *, state: CanonicalState) -> str:
        ticket = self.ledger.create_ticket(
            title=title,
            state=state,
            external_id=f"external-{title}",
            contract=self.contract(),
        )
        self.ledger.bind_runtime(ticket, str(self.root), "a" * 40)
        return ticket

    def validation_candidate(self, title: str = "validate") -> str:
        ticket = self.ticket(title, state=CanonicalState.IMPLEMENTING)
        artifact = self.root / f"{title}-implementation.json"
        artifact.write_text("{}", encoding="utf-8")
        self.ledger.ensure_attempt(ticket, 1)
        self.ledger.record_model_stage(
            ticket,
            1,
            "implementation",
            purpose="implementation",
            adapter="fixture",
            request_hash="request",
            response_artifact=str(artifact),
            worktree_path=str(self.root),
            base_sha="a" * 40,
            diff_hash="diff",
        )
        return ticket

    def test_policy_rank_is_explicit_complete_and_unique(self) -> None:
        expected = (
            "generated_projection",
            "state_projection",
            "evidence_comment",
            "recovery",
            "implementation",
            "validation",
            "review",
            "repair_routing",
            "triage",
            "acceptance",
            "git_integration",
            "completion",
            "worktree_cleanup",
            "native_dependency_graph",
            "native_dependency_release",
            "tranche_checkpoint",
            "paid_checkpoint",
            "paid_escalation",
            "next_tranche_materialize",
            "next_tranche_activation",
            "dependency_readiness",
        )
        self.assertEqual(SCHEDULER_STAGE_ORDER, expected)
        self.assertEqual(len(set(SCHEDULER_STAGE_ORDER)), len(SCHEDULER_STAGE_ORDER))
        self.assertEqual([scheduler_stage_rank(stage) for stage in expected], list(range(len(expected))))
        with self.assertRaises(KeyError):
            scheduler_stage_rank("unknown")

    def test_projection_precedence_is_generated_then_state_then_comment(self) -> None:
        state_ticket = self.ticket("project-state", state=CanonicalState.DRAFT)
        self.ledger.transition(state_ticket, CanonicalState.READY_LOCAL)
        planned = self.ledger.plan_projection(state_ticket, evidence="projection evidence")
        self.assertIsNotNone(planned)

        generated_ticket = self.ticket("generated", state=CanonicalState.DRAFT)
        event_id = self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) "
            "VALUES ('ticket',?,'generated_microticket_created','system','test','{}',1) RETURNING id",
            (generated_ticket,),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation) "
            "VALUES (?,?,'draft','{}','generated-ordering',0,'create_microticket')",
            (generated_ticket, event_id),
        )

        first = preview_next(self.ledger, now=100)
        self.assertEqual((first.next_stage, first.ticket_id), ("generated_projection", generated_ticket))

        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET acknowledged_at=1,external_task_id='ext-generated' "
            "WHERE ticket_id=? AND operation='create_microticket'",
            (generated_ticket,),
        )
        second = preview_next(self.ledger, now=100)
        self.assertEqual((second.next_stage, second.ticket_id), ("state_projection", state_ticket))

        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET acknowledged_at=1 WHERE ticket_id=? AND operation='set_state'",
            (state_ticket,),
        )
        third = preview_next(self.ledger, now=100)
        self.assertEqual((third.next_stage, third.ticket_id), ("evidence_comment", state_ticket))

    def test_fresh_implementation_precedes_fresh_validation_across_tickets(self) -> None:
        validation_ticket = self.validation_candidate()
        implementation_ticket = self.ticket("implement", state=CanonicalState.READY_LOCAL)

        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id), ("implementation", implementation_ticket))

        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="ordering",
            lease_seconds=30,
            clock=lambda: 100,
            implementation_runner=lambda ticket_id: (_ for _ in ()).throw(ImplementationChosen(ticket_id)),
            validation_runner=lambda ticket_id: (_ for _ in ()).throw(AssertionError(f"validation ran before implementation: {ticket_id}")),
        )
        with self.assertRaisesRegex(ImplementationChosen, implementation_ticket):
            scheduler.process_next()
        self.assertEqual(self.ledger.get_ticket(validation_ticket)["state"], CanonicalState.IMPLEMENTING.value)

    def test_expired_recovery_precedes_fresh_implementation(self) -> None:
        validation_ticket = self.validation_candidate("recover-validation")
        claim = self.ledger.claim_next_scheduler_validation("dead-worker", lease_seconds=1, now=100)
        self.assertIsNotNone(claim)
        claim_id = str(claim["claim_id"])
        implementation_ticket = self.ticket("fresh-implementation", state=CanonicalState.READY_LOCAL)

        preview = preview_next(self.ledger, now=102)
        self.assertEqual((preview.next_stage, preview.ticket_id, preview.claim_id, preview.reconciliation_action), ("validation", validation_ticket, claim_id, "retry"))

        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="ordering-recovery",
            lease_seconds=30,
            clock=lambda: 102,
            implementation_runner=lambda ticket_id: (_ for _ in ()).throw(AssertionError(f"fresh implementation bypassed recovery: {ticket_id}")),
            validation_runner=lambda ticket_id: (_ for _ in ()).throw(ValidationChosen(ticket_id)),
        )
        with self.assertRaisesRegex(ValidationChosen, validation_ticket):
            scheduler.process_next()
        self.assertEqual(self.ledger.get_ticket(implementation_ticket)["state"], CanonicalState.READY_LOCAL.value)

    def test_external_projection_precedes_recovery_and_recovery_precedes_fresh_work(self) -> None:
        validation_ticket = self.validation_candidate("recover-behind-projection")
        claim = self.ledger.claim_next_scheduler_validation("dead-worker", lease_seconds=1, now=100)
        self.assertIsNotNone(claim)
        claim_id = str(claim["claim_id"])
        fresh_ticket = self.ticket("fresh-behind-recovery", state=CanonicalState.READY_LOCAL)

        projected_ticket = self.ticket("project-before-recovery", state=CanonicalState.DRAFT)
        self.ledger.transition(projected_ticket, CanonicalState.READY_LOCAL)
        bundle = self.ledger.plan_projection(projected_ticket, evidence="projection before recovery")
        self.assertIsNotNone(bundle)

        first = preview_next(self.ledger, now=102)
        self.assertEqual((first.next_stage, first.ticket_id), ("state_projection", projected_ticket))

        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET acknowledged_at=1 WHERE ticket_id=? AND operation='set_state'",
            (projected_ticket,),
        )
        second = preview_next(self.ledger, now=102)
        self.assertEqual((second.next_stage, second.ticket_id), ("evidence_comment", projected_ticket))

        self.ledger.connection.execute(
            "UPDATE evidence_comment_outbox SET status='delivered',delivered_at=1 WHERE ticket_id=?",
            (projected_ticket,),
        )
        third = preview_next(self.ledger, now=102)
        self.assertEqual((third.next_stage, third.ticket_id, third.claim_id), ("validation", validation_ticket, claim_id))
        self.assertNotEqual(third.ticket_id, fresh_ticket)

    def test_independent_scheduler_views_choose_same_stage_and_ticket(self) -> None:
        self.validation_candidate("validate-view")
        implementation_ticket = self.ticket("implement-view", state=CanonicalState.READY_LOCAL)
        second = Ledger(self.database)
        try:
            first_preview = preview_next(self.ledger, now=100)
            second_preview = preview_next(second, now=100)
            self.assertEqual(first_preview, second_preview)
            self.assertEqual((first_preview.next_stage, first_preview.ticket_id), ("implementation", implementation_ticket))
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
