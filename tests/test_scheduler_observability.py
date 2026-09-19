from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.cli import register_cli, run_command
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import preview_next, scheduler_observability
from local_first_orchestrator.states import CanonicalState


class SchedulerObservabilityTests(unittest.TestCase):
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
            "objective": "prove scheduler observability",
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
        }

    def ticket(self, title: str, state: CanonicalState = CanonicalState.READY_LOCAL) -> str:
        ticket = self.ledger.create_ticket(title=title, state=state, external_id=f"external-{title}", contract=self.contract())
        self.ledger.bind_runtime(ticket, str(self.root), "a" * 40)
        return ticket

    def test_snapshot_reports_next_stage_and_is_read_only(self) -> None:
        ticket = self.ticket("next")
        before = self.ledger.connection.total_changes
        snapshot = scheduler_observability(self.ledger, now=100)
        after = self.ledger.connection.total_changes
        self.assertEqual(before, after)
        self.assertEqual(snapshot["next_stage"], "implementation")
        self.assertEqual(snapshot["ticket_id"], ticket)
        self.assertIsNone(snapshot["current_stage"])
        self.assertIsNone(snapshot["claim"])
        self.assertIsNone(snapshot["reconciliation"])
        self.assertEqual(snapshot["pending_effects"], {"generated_projection": 0, "state_projection": 0, "evidence_comment": 0})

    def test_expired_model_claim_reports_exact_reconciliation_boundary(self) -> None:
        ticket = self.ticket("model-stop")
        claim = self.ledger.claim_next_scheduler_implementation("dead-worker", lease_seconds=1, now=100)
        self.assertIsNotNone(claim)
        self.ledger.begin_scheduler_claim_effect(claim["claim_id"], "dead-worker", now=100)
        self.ledger.ensure_attempt(ticket, 1)
        self.ledger.start_model_invocation(
            invocation_id="inv-stop",
            ticket_id=ticket,
            attempt_number=1,
            stage="implementation",
            provider="provider",
            model="model",
            packet_hash="packet",
            worktree_path=str(self.root),
            timeout_seconds=30,
        )
        snapshot = scheduler_observability(self.ledger, now=102)
        self.assertEqual(snapshot["next_stage"], "reconciliation_required")
        self.assertEqual(snapshot["current_stage"], "implementation")
        self.assertEqual(snapshot["ticket_id"], ticket)
        self.assertEqual(snapshot["attempt_number"], 1)
        self.assertEqual(snapshot["claim"]["claim_id"], claim["claim_id"])
        self.assertEqual(snapshot["claim"]["lease_owner"], "dead-worker")
        self.assertEqual(snapshot["claim"]["lease_expires_at"], 101)
        self.assertEqual(snapshot["reconciliation"]["state"], "started_external_outcome_unknown")
        self.assertEqual(snapshot["reconciliation"]["action"], "stop")
        self.assertEqual(snapshot["reconciliation"]["evidence_kind"], "model_invocations")
        self.assertIn("automatic retry", snapshot["reconciliation"]["reason"])
        self.assertEqual(snapshot["model_invocation"]["invocation_id"], "inv-stop")
        self.assertEqual(snapshot["model_invocation"]["status"], "started")

    def test_snapshot_exposes_artifact_review_git_and_pending_effect_identities(self) -> None:
        ticket = self.ticket("artifacts", CanonicalState.IMPLEMENTING)
        self.ledger.ensure_attempt(ticket, 1)
        artifact = self.root / "validation.json"
        artifact.write_text("{}", encoding="utf-8")
        self.ledger.record_runtime_stage(ticket, "validation_pass", "validated", attempt_number=1, artifact_path=str(artifact), artifact_sha256="b" * 64, base_sha="a" * 40)
        self.ledger.connection.execute(
            "INSERT INTO review_results(ticket_id,attempt_number,verdict,payload_json,created_at) VALUES (?,1,'pass','{}',2)",
            (ticket,),
        )
        review_id = self.ledger.connection.execute("SELECT id FROM review_results WHERE ticket_id=?", (ticket,)).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO accepted_candidates(ticket_id,attempt_number,candidate_fingerprint,base_sha,worktree_path,implementation_artifact,implementation_artifact_sha256,validation_artifact,validation_artifact_sha256,review_artifact,review_artifact_sha256,review_result_id,evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, 1, "diff", "a" * 40, str(self.root), "implementation.json", "c" * 64, str(artifact), "b" * 64, "review.json", "d" * 64, review_id, "e" * 64, 3),
        )
        self.ledger.connection.execute(
            "INSERT INTO git_commit_intents(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,commit_message,status,commit_sha,created_at,completed_at) VALUES (?,1,?,'diff',?,?, 'commit','completed',?,4,5)",
            (ticket, "e" * 64, "a" * 40, str(self.root), "f" * 40),
        )
        self.ledger.connection.execute(
            "INSERT INTO git_commit_evidence(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,branch,commit_message,commit_sha,tranche_id,integration_head_before,integration_head_after,created_at) VALUES (?,1,?,'diff',?,?,'branch','commit',?,NULL,?,?,5)",
            (ticket, "e" * 64, "a" * 40, str(self.root), "f" * 40, "a" * 40, "f" * 40),
        )
        event_id = self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,'state_transition','system','obs','{}',6) RETURNING id",
            (ticket,),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation) VALUES (?,?,'done','{}','obs-state',6,'set_state')",
            (ticket, event_id),
        )
        snapshot = scheduler_observability(self.ledger, now=100)
        self.assertEqual(snapshot["latest_runtime_stage"]["artifact_path"], str(artifact))
        self.assertEqual(snapshot["latest_runtime_stage"]["artifact_sha256"], "b" * 64)
        self.assertEqual(snapshot["review_result"]["id"], review_id)
        self.assertEqual(snapshot["review_result"]["verdict"], "pass")
        self.assertEqual(snapshot["accepted_candidate"]["evidence_hash"], "e" * 64)
        self.assertEqual(snapshot["git"]["evidence"]["commit_sha"], "f" * 40)
        self.assertEqual(snapshot["pending_effects"]["state_projection"], 1)

    def test_replacement_acceptance_drives_preview_and_snapshot_authority(self) -> None:
        ticket = self.ticket("replacement", CanonicalState.ACCEPTED)
        self.ledger.connection.execute(
            "INSERT INTO accepted_candidates(ticket_id,attempt_number,candidate_fingerprint,base_sha,worktree_path,implementation_artifact,implementation_artifact_sha256,validation_artifact,validation_artifact_sha256,review_artifact,review_artifact_sha256,review_result_id,evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, 1, "old-diff", "a" * 40, str(self.root), "old-implementation.json", "1" * 64, "old-validation.json", "2" * 64, "old-review.json", "3" * 64, 1, "4" * 64, 1),
        )
        self.ledger.connection.execute(
            "INSERT INTO accepted_candidate_invalidations(ticket_id,invalidated_attempt_number,invalidated_evidence_hash,corrected_candidate_fingerprint,operator_id,reason,created_at) VALUES (?,?,?,?,?,?,?)",
            (ticket, 1, "4" * 64, "new-diff", "operator", "rebind", 2),
        )
        self.ledger.connection.execute(
            "INSERT INTO accepted_candidate_replacements(ticket_id,attempt_number,candidate_fingerprint,base_sha,worktree_path,implementation_artifact,implementation_artifact_sha256,validation_artifact,validation_artifact_sha256,review_artifact,review_artifact_sha256,review_result_id,evidence_hash,supersedes_evidence_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, 2, "new-diff", "a" * 40, str(self.root), "new-implementation.json", "5" * 64, "new-validation.json", "6" * 64, "new-review.json", "7" * 64, 2, "8" * 64, "4" * 64, 3),
        )

        first = preview_next(self.ledger, now=100)
        snapshot = scheduler_observability(self.ledger, now=100)
        self.assertEqual((first.next_stage, first.ticket_id), ("git_integration", ticket))
        self.assertEqual(snapshot["accepted_candidate"]["attempt_number"], 2)
        self.assertEqual(snapshot["accepted_candidate"]["candidate_fingerprint"], "new-diff")
        self.assertEqual(snapshot["accepted_candidate"]["evidence_hash"], "8" * 64)

        self.ledger.connection.execute(
            "INSERT INTO git_commit_intents(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,commit_message,status,commit_sha,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, 2, "8" * 64, "new-diff", "a" * 40, str(self.root), "commit", "completed", "f" * 40, 4, 5),
        )
        self.ledger.connection.execute(
            "INSERT INTO git_commit_evidence(ticket_id,attempt_number,accepted_evidence_hash,candidate_fingerprint,base_sha,worktree_path,branch,commit_message,commit_sha,tranche_id,integration_head_before,integration_head_after,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, 2, "8" * 64, "new-diff", "a" * 40, str(self.root), "branch", "commit", "f" * 40, None, "a" * 40, "f" * 40, 5),
        )
        second = preview_next(self.ledger, now=100)
        self.assertEqual((second.next_stage, second.ticket_id), ("completion", ticket))

    def test_paid_claim_reports_reservation_state(self) -> None:
        ticket = self.ticket("paid")
        self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F','F','active',1,1)")
        self.ledger.connection.execute(
            "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,candidate_identity_json,created_at,updated_at) VALUES ('paid-claim',?,'paid_checkpoint','claimed','paid-worker',200,1,'{}',1,1)",
            (ticket,),
        )
        self.ledger.connection.execute(
            "INSERT INTO paid_reservations(id,feature_id,purpose,request_key,status,created_at,updated_at) VALUES ('reservation','F','integration_checkpoint','paid-claim','completed',1,2)"
        )
        snapshot = scheduler_observability(self.ledger, now=100)
        self.assertEqual(snapshot["current_stage"], "paid_checkpoint")
        self.assertEqual(snapshot["paid_reservation"]["id"], "reservation")
        self.assertEqual(snapshot["paid_reservation"]["status"], "completed")
        self.assertEqual(snapshot["paid_reservation"]["request_key"], "paid-claim")

    def test_status_cli_emits_scheduler_detail_only_when_requested(self) -> None:
        self.ticket("cli")
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["--database", str(self.database), "status", "--scheduler-detail"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_command(args), 0)
        payload = json.loads(output.getvalue())
        self.assertIn("scheduler_detail", payload)
        self.assertEqual(payload["scheduler_detail"]["next_stage"], "implementation")

        args = parser.parse_args(["--database", str(self.database), "status"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_command(args), 0)
        payload = json.loads(output.getvalue())
        self.assertNotIn("scheduler_detail", payload)


if __name__ == "__main__":
    unittest.main()
