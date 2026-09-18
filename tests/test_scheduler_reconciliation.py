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

    def test_paused_operator_can_release_replay_safe_abandoned_claim_but_not_ambiguous_model_effect(self) -> None:
        replayable = self.claim("native_dependency_release", started=2, expires=500)
        self.assertEqual(self.ledger.scheduler_reconciliation(replayable).action, ReconciliationAction.REPLAY)
        with self.assertRaisesRegex(PermissionError, "requires Local First paused"):
            self.ledger.release_abandoned_scheduler_claim(replayable, operator_id="operator", reason="worker exited", now=100)
        self.ledger.pause("operator", reason="recover abandoned claim")
        released = self.ledger.release_abandoned_scheduler_claim(replayable, operator_id="operator", reason="worker exited", now=100)
        self.assertIsNone(released["lease_owner"])
        self.assertEqual(int(released["lease_expires_at"]), 99)
        event = self.ledger.connection.execute("SELECT event_type,payload_json FROM events WHERE entity_type='ticket' AND entity_id=? ORDER BY id DESC LIMIT 1", (self.ticket,)).fetchone()
        self.assertEqual(event["event_type"], "scheduler_stage_claim_abandoned_released")
        self.assertEqual(json.loads(event["payload_json"])["reconciliation_action"], "replay")

        self.ledger.connection.execute("DELETE FROM scheduler_stage_claims")
        ambiguous = self.claim("implementation:1", started=2, identity={"attempt_number": 1}, expires=500)
        self.ledger.connection.execute(
            "INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status) VALUES ('inv-ambiguous',?,1,'implementation','p','m','hash','/tmp/w',30,2,'started')",
            (self.ticket,),
        )
        with self.assertRaisesRegex(PermissionError, "not replay-safe"):
            self.ledger.release_abandoned_scheduler_claim(ambiguous, operator_id="operator", reason="must remain protected", now=100)

    def test_provider_rejection_defer_redacts_error_detail_in_audit_event(self) -> None:
        claim_id = self.claim("paid_checkpoint", started=2, expires=500)
        row = self.ledger.defer_scheduler_paid_claim_after_provider_rejection(
            claim_id,
            "dead",
            retry_after_at=200,
            error_kind="rate_limited",
            error_detail="429 api_key=super-secret Authorization: Bearer bearer-secret",
            now=100,
        )
        self.assertIn("[REDACTED]", str(row["last_error"]))
        self.assertNotIn("super-secret", str(row["last_error"]))
        event = self.ledger.connection.execute(
            "SELECT payload_json FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='scheduler_paid_provider_rejected_deferred' ORDER BY id DESC LIMIT 1",
            (self.ticket,),
        ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertIn("[REDACTED]", payload["error_detail"])
        self.assertNotIn("super-secret", payload["error_detail"])
        self.assertNotIn("bearer-secret", payload["error_detail"])

    def test_review_claim_release_requires_exact_unconsumed_retry_authorization(self) -> None:
        self.ledger.connection.execute("UPDATE tickets SET state='local_review' WHERE id=?", (self.ticket,))
        identity = {"attempt_number": 1, "implementation_diff_hash": "fp"}
        claim_id = self.claim("review:1", started=2, identity=identity, expires=500)
        runtime_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        self.ledger.connection.execute(
            "INSERT INTO review_candidates(ticket_id,attempt_number,candidate_fingerprint,validation_evidence,implementation_invocation_id,runtime_identity_json,status,last_outcome,created_at,updated_at) VALUES (?,1,'fp','ok','impl',?,'review_infrastructure_failed','review_process_error',1,1)",
            (self.ticket, runtime_identity),
        )
        self.ledger.connection.execute(
            "INSERT INTO model_invocations(invocation_id,ticket_id,attempt_number,stage,provider,model,packet_hash,worktree_path,timeout_seconds,started_at,status,completed_at,error_json) VALUES ('review-failed',?,1,'review','p','m','packet','packet-only',30,2,'process_error',3,'{}')",
            (self.ticket,),
        )
        self.ledger.pause("operator", reason="authorize review retry")
        with self.assertRaisesRegex(PermissionError, "unconsumed retry authorization"):
            self.ledger.release_abandoned_scheduler_claim(claim_id, operator_id="operator", reason="not yet authorized", now=100)
        authorization = self.ledger.authorize_review_retry(self.ticket, operator_id="operator", candidate_fingerprint="fp")
        self.assertIsNone(authorization["consumed_invocation_id"])

        released = self.ledger.release_abandoned_scheduler_claim(claim_id, operator_id="operator", reason="authorized review retry", now=100)

        self.assertIsNone(released["lease_owner"])
        event = self.ledger.connection.execute("SELECT payload_json FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='scheduler_stage_claim_abandoned_released' ORDER BY id DESC LIMIT 1", (self.ticket,)).fetchone()
        self.assertEqual(json.loads(event["payload_json"])["reconciliation_action"], "authorized_review_retry")

    def test_paused_operator_can_retire_completed_validation_claim_for_retired_attempt(self) -> None:
        claim_id = self.claim("validation:1", started=2, completed=3, expires=50)
        self.ledger.connection.execute(
            "INSERT INTO failed_attempt_reconciliations(ticket_id,retired_attempt_number,classification,previous_ticket_state,resulting_ticket_state,operator_id,runtime_identity_json,retry_base_sha,prospective_next_attempt_number,cleanup_required,forensic_artifact_paths_json,reconciled_at) VALUES (?,1,'validation_failure','verifying','ready_local','operator','{}',?,2,0,'[]',4)",
            (self.ticket, "a" * 40),
        )
        with self.assertRaisesRegex(PermissionError, "requires Local First paused"):
            self.ledger.retire_historical_scheduler_claim(claim_id, operator_id="operator", reason="historical residue", now=100)
        self.ledger.pause("operator", reason="maintenance")
        retired = self.ledger.retire_historical_scheduler_claim(claim_id, operator_id="operator", reason="historical residue", now=100)
        self.assertEqual((retired["status"], retired["retirement_status"], retired["proof_kind"]), ("completed", "retired", "retired_attempt"))
        self.assertIsNone(retired["lease_owner"])
        self.assertIsNone(retired["lease_expires_at"])
        self.assertEqual(int(retired["finalized_at"]), 100)
        event = self.ledger.connection.execute("SELECT event_type,payload_json FROM events WHERE entity_id=? ORDER BY id DESC LIMIT 1", (self.ticket,)).fetchone()
        self.assertEqual(event["event_type"], "scheduler_stage_historical_retired")
        self.assertEqual(json.loads(event["payload_json"])["proof_kind"], "retired_attempt")

    def test_paused_operator_can_retire_completed_review_claim_after_terminal_application(self) -> None:
        self.ledger.connection.execute("UPDATE tickets SET state='done' WHERE id=?", (self.ticket,))
        claim_id = self.claim("review:2", started=2, completed=3, identity={"attempt_number": 2}, expires=50)
        self.ledger.connection.execute(
            "INSERT INTO review_results(ticket_id,attempt_number,verdict,payload_json,created_at) VALUES (?,2,'repair','{}',4)",
            (self.ticket,),
        )
        self.ledger.pause("operator", reason="maintenance")
        result = self.ledger.retire_historical_scheduler_claims(ticket_id=self.ticket, operator_id="operator", reason="historical residue", now=100)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["retired"]), 1)
        row = result["retired"][0]
        self.assertEqual((row["claim_id"], row["proof_kind"], row["status"]), (claim_id, "terminal_review_applied", "completed"))

    def test_historical_retirement_refuses_completed_effect_without_terminal_supersession(self) -> None:
        claim_id = self.claim("validation:1", started=2, completed=3, expires=50)
        self.ledger.pause("operator", reason="maintenance")
        with self.assertRaisesRegex(PermissionError, "no supported terminal supersession proof"):
            self.ledger.retire_historical_scheduler_claim(claim_id, operator_id="operator", reason="historical residue", now=100)

    def test_bulk_historical_retirement_reports_unsupported_claims_as_skipped(self) -> None:
        supported = self.claim("validation:1", started=2, completed=3, expires=50)
        unsupported = self.claim("completion:2", started=4, completed=5, expires=50)
        self.ledger.connection.execute(
            "INSERT INTO failed_attempt_reconciliations(ticket_id,retired_attempt_number,classification,previous_ticket_state,resulting_ticket_state,operator_id,runtime_identity_json,retry_base_sha,prospective_next_attempt_number,cleanup_required,forensic_artifact_paths_json,reconciled_at) VALUES (?,1,'validation_failure','verifying','ready_local','operator','{}',?,2,0,'[]',6)",
            (self.ticket, "a" * 40),
        )
        self.ledger.pause("operator", reason="maintenance")
        result = self.ledger.retire_historical_scheduler_claims(ticket_id=self.ticket, operator_id="operator", reason="historical residue", now=100)
        self.assertEqual([row["claim_id"] for row in result["retired"]], [supported])
        self.assertEqual(result["skipped"], [{"claim_id": unsupported, "stage": "completion:2", "reason": "scheduler claim has no supported terminal supersession proof"}])
        remaining = self.ledger.connection.execute("SELECT status,finalized_at FROM scheduler_stage_claims WHERE claim_id=?", (unsupported,)).fetchone()
        self.assertEqual(remaining["status"], "claimed")
        self.assertIsNone(remaining["finalized_at"])

    def test_external_id_resolves_exact_internal_ticket(self) -> None:
        self.ledger.connection.execute("UPDATE tickets SET external_id='t-generated' WHERE id=?", (self.ticket,))
        self.assertEqual(self.ledger.ticket_id_for_external_id("t-generated"), self.ticket)
        with self.assertRaises(KeyError):
            self.ledger.ticket_id_for_external_id("missing")

    def test_paid_unknown_outcome_stops_but_completed_paid_call_reconciles(self) -> None:
        self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F','F','active',1,1)")
        claim_id = self.claim("paid_checkpoint", started=2)
        self.ledger.connection.execute("INSERT INTO paid_reservations(id,feature_id,purpose,request_key,status,created_at,updated_at) VALUES ('r1','F','integration_checkpoint',?,'unknown_outcome',1,1)", (claim_id,))
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.action, decision.evidence["status"]), (ReconciliationAction.STOP, "unknown_outcome"))

        self.ledger.connection.execute("UPDATE paid_reservations SET status='completed' WHERE id='r1'")
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.state, decision.action), (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE))

    def test_confirmed_paid_provider_rejection_defers_claim_until_retry_deadline_and_replays(self) -> None:
        self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F','F','active',1,1)")
        claim_id = self.claim("paid_checkpoint", started=2, expires=150)
        self.ledger.connection.execute(
            "INSERT INTO paid_reservations(id,feature_id,purpose,request_key,status,provider_failure_count,provider_retry_after_at,provider_error_kind,provider_error_detail,provider_retryable,created_at,updated_at) VALUES ('r-retry','F','integration_checkpoint',?,'provider_rejected',1,200,'rate_limited','429',1,1,1)",
            (claim_id,),
        )
        deferred = self.ledger.defer_scheduler_paid_claim_after_provider_rejection(
            claim_id,
            "dead",
            retry_after_at=200,
            error_kind="rate_limited",
            error_detail="429",
            now=100,
        )
        self.assertIsNone(deferred["lease_owner"])
        self.assertEqual(int(deferred["lease_expires_at"]), 200)
        decision = self.ledger.scheduler_reconciliation(claim_id)
        self.assertEqual((decision.action, decision.evidence["status"], decision.evidence["provider_error_kind"]), (ReconciliationAction.REPLAY, "provider_rejected", "rate_limited"))
        self.assertIsNone(self.ledger.next_scheduler_reconciliation(now=199, ticket_id=self.ticket))
        due = self.ledger.next_scheduler_reconciliation(now=200, ticket_id=self.ticket)
        self.assertIsNotNone(due)
        self.assertEqual((due.claim_id, due.action), (claim_id, ReconciliationAction.REPLAY))

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
