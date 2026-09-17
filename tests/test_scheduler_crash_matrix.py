from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.reconciliation import (
    CRASH_BOUNDARY_ACTIONS,
    ReconciliationAction,
    ReconciliationState,
    SCHEDULER_CRASH_POLICIES,
)
from local_first_orchestrator.scheduler import SCHEDULER_STAGE_ORDER
from local_first_orchestrator.states import CanonicalState


class SchedulerCrashMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(
            title="crash-matrix",
            state=CanonicalState.READY_LOCAL,
            external_id="external-crash-matrix",
            contract={
                "objective": "exercise scheduler crash matrix",
                "criterion_ids": ["AC-1"],
                "primary_symbol": "app.py::value",
                "allowed_files": ["app.py"],
                "forbidden_changes": ["no unrelated changes"],
                "patch_budget": {"max_files": 1, "max_changed_lines": 10},
                "verification": {"commands": [["python", "-c", "pass"]]},
                "risk": "low",
                "review_required": True,
                "max_attempts": 2,
                "dependencies": [],
            },
        )
        self.ledger.bind_runtime(self.ticket, str(self.root), "a" * 40)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def claim(self, stage: str, *, started: int | None = None, completed: int | None = None, status: str = "claimed", identity: dict | None = None) -> str:
        claim_id = f"claim-{stage.replace(':','-')}"
        self.ledger.connection.execute(
            "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,result_json,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (
                claim_id,
                self.ticket,
                stage,
                status,
                None if status == "completed" else "dead-worker",
                None if status == "completed" else 50,
                json.dumps({"ok": True}, sort_keys=True, separators=(",", ":")) if completed is not None else None,
                started,
                completed,
                completed if status == "completed" else None,
                json.dumps(identity or {}, sort_keys=True, separators=(",", ":")),
                1,
                1,
            ),
        )
        return claim_id

    def test_every_scheduler_work_class_has_exactly_one_crash_policy(self) -> None:
        expected = set(SCHEDULER_STAGE_ORDER) - {"recovery"}
        self.assertEqual(set(SCHEDULER_CRASH_POLICIES), expected)
        for stage, policy in SCHEDULER_CRASH_POLICIES.items():
            self.assertEqual(policy.stage, stage)
            self.assertIn(policy.started_action, {ReconciliationAction.REPLAY, ReconciliationAction.STOP})
            self.assertEqual(policy.external_completed_action, ReconciliationAction.RECONCILE)
            self.assertEqual(policy.downstream_incomplete_action, ReconciliationAction.RESUME)
            self.assertEqual(policy.finalized_action, ReconciliationAction.RESUME)
            self.assertTrue(policy.authority)

    def test_common_durable_boundaries_have_one_scheduler_wide_action(self) -> None:
        self.assertEqual(
            CRASH_BOUNDARY_ACTIONS,
            (
                (ReconciliationState.NOT_STARTED, ReconciliationAction.RETRY),
                (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE),
                (ReconciliationState.LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE, ReconciliationAction.RESUME),
                (ReconciliationState.FULLY_FINALIZED, ReconciliationAction.RESUME),
            ),
        )

    def test_unknown_model_and_paid_effects_are_fail_closed(self) -> None:
        for stage in ("implementation", "review", "ticket_paid_escalation", "triage", "paid_checkpoint", "paid_escalation"):
            with self.subTest(stage=stage):
                self.assertEqual(SCHEDULER_CRASH_POLICIES[stage].started_action, ReconciliationAction.STOP)
        self.assertEqual(SCHEDULER_CRASH_POLICIES["implementation"].authority, "model_invocations")
        self.assertEqual(SCHEDULER_CRASH_POLICIES["paid_checkpoint"].authority, "paid_reservations")

    def test_replayable_stages_are_explicit_and_never_silently_stop(self) -> None:
        replayable = set(SCHEDULER_CRASH_POLICIES) - {"implementation", "review", "ticket_paid_escalation", "triage", "paid_checkpoint", "paid_escalation"}
        for stage in sorted(replayable):
            with self.subTest(stage=stage):
                self.assertEqual(SCHEDULER_CRASH_POLICIES[stage].started_action, ReconciliationAction.REPLAY)

    def test_claim_before_effect_retries_and_completed_effect_reconciles_without_rerun(self) -> None:
        claim_id = self.claim("validation:1")
        before = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((before.state, before.action), (ReconciliationState.NOT_STARTED, ReconciliationAction.RETRY))

        self.ledger.connection.execute("DELETE FROM scheduler_stage_claims WHERE claim_id=?", (claim_id,))
        claim_id = self.claim("validation:1", started=2, completed=3)
        after = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual(
            (after.state, after.action),
            (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE),
        )

    def test_model_started_without_completion_stops_and_completed_invocation_reconciles(self) -> None:
        claim_id = self.claim("implementation:1", started=2, identity={"attempt_number": 1})
        self.ledger.connection.execute(
            "INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status) VALUES ('inv',?,1,'implementation','provider','model','packet',?,30,2,'started')",
            (self.ticket, str(self.root)),
        )
        unknown = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual(
            (unknown.state, unknown.action, unknown.evidence_kind),
            (ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "model_invocations"),
        )

        self.ledger.connection.execute("UPDATE model_invocations SET status='completed',completed_at=3,model_artifact='artifact.json' WHERE invocation_id='inv'")
        completed = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual(
            (completed.state, completed.action, completed.evidence_kind),
            (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE, "model_invocations"),
        )

    def test_finalized_stage_with_pending_projection_resumes_only_downstream_work(self) -> None:
        claim_id = self.claim("completion:1", started=2, completed=3, status="completed")
        event_id = self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,'state_transition','system','crash-matrix','{}',1) RETURNING id",
            (self.ticket,),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at) VALUES (?,?,'done','{}','crash-matrix-projection',1)",
            (self.ticket, event_id),
        )
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual(
            (decision.state, decision.action, decision.evidence_kind),
            (ReconciliationState.LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE, ReconciliationAction.RESUME, "outbox"),
        )


if __name__ == "__main__":
    unittest.main()
