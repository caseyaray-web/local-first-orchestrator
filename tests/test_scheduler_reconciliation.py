from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.reconciliation import ReconciliationAction, ReconciliationState
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next


class Board:
    timeout_seconds = 1
    def set_state(self, *args, **kwargs): return None
    def find_comment_marker(self, *args, **kwargs): return "not_found"
    def deliver_comment(self, *args, **kwargs): return None
    def create_microticket(self, *args, **kwargs): return "external"


class SchedulerReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="ticket")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def claim(self, stage: str, *, started: int | None = None, completed: int | None = None, status: str = "claimed", identity: dict | None = None, expires: int = 50) -> str:
        claim_id = f"claim-{stage.replace(':','-')}"
        self.ledger.connection.execute(
            "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,result_json,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (
                claim_id,
                self.ticket,
                stage,
                status,
                None if status == "completed" else "dead",
                None if status == "completed" else expires,
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

    def test_five_reconciliation_states_are_derived_from_durable_records(self) -> None:
        not_started = self.claim("validation:1")
        decision = self.ledger.scheduler_reconciliation(not_started)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.NOT_STARTED, ReconciliationAction.RETRY))

        self.ledger.connection.execute("DELETE FROM scheduler_stage_claims")
        effect_done = self.claim("validation:1", started=2, completed=3)
        decision = self.ledger.scheduler_reconciliation(effect_done)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE))

        self.ledger.connection.execute("DELETE FROM scheduler_stage_claims")
        finalized = self.claim("validation:1", started=2, completed=3, status="completed")
        decision = self.ledger.scheduler_reconciliation(finalized)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.FULLY_FINALIZED, ReconciliationAction.RESUME))

        event_id = self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,'state_transition','system','test','{}',1) RETURNING id",
            (self.ticket,),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at) VALUES (?,?,'draft','{}','pending-projection',1)",
            (self.ticket,event_id),
        )
        decision = self.ledger.scheduler_reconciliation(finalized)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE, ReconciliationAction.RESUME))

    def test_started_model_invocation_stops_automatic_retry(self) -> None:
        claim_id = self.claim("implementation:1", started=2, identity={"attempt_number": 1})
        self.ledger.connection.execute(
            "INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status) VALUES ('inv',?,1,'implementation','p','m','hash','/tmp/w',30,2,'started')",
            (self.ticket,),
        )
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.state, decision.action, decision.evidence_kind), (ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.STOP, "model_invocations"))
        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.claim_id, preview.reconciliation_action), ("reconciliation_required", claim_id, "stop"))
        with self.assertRaisesRegex(RuntimeError, "execution_reconciliation_required"):
            ProcessNextScheduler(self.ledger, Board(), worker_id="worker", lease_seconds=30, clock=lambda:100).process_next()

    def test_paid_unknown_outcome_stops_but_completed_paid_call_reconciles(self) -> None:
        self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F','F','active',1,1)")
        claim_id = self.claim("paid_checkpoint", started=2)
        self.ledger.connection.execute("INSERT INTO paid_reservations(id,feature_id,purpose,request_key,status,created_at,updated_at) VALUES ('r1','F','integration_checkpoint',?,'unknown_outcome',1,1)", (claim_id,))
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.action, decision.evidence["status"]), (ReconciliationAction.STOP, "unknown_outcome"))

        self.ledger.connection.execute("UPDATE paid_reservations SET status='completed' WHERE id='r1'")
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE))

    def test_git_intent_without_commit_evidence_is_exact_replay(self) -> None:
        claim_id = self.claim("git_integration:1", started=2)
        self.ledger.connection.execute(
            "INSERT INTO git_commit_intents(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,commit_message,status,created_at) VALUES (?,1,'e','f','base','/tmp/w','msg','started',1)",
            (self.ticket,),
        )
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.state, decision.action, decision.evidence_kind), (ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY, "git_commit_intents"))

    def test_deterministic_local_started_stage_is_replayable(self) -> None:
        claim_id = self.claim("completion:1", started=2)
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.STARTED_EXTERNAL_OUTCOME_UNKNOWN, ReconciliationAction.REPLAY))


if __name__ == "__main__":
    unittest.main()
