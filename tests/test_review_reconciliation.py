from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class ReviewReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="review")
        for state in (CanonicalState.READY_LOCAL, CanonicalState.IMPLEMENTING, CanonicalState.VERIFYING, CanonicalState.LOCAL_REVIEW): self.ledger.transition(self.ticket, state)
        self.fp = "a" * 64
        self.ledger.freeze_review_candidate(self.ticket, 2, candidate_fingerprint=self.fp, validation_evidence="passed", implementation_invocation_id=None, runtime_identity={})

    def tearDown(self): self.ledger.close(); self.temp.cleanup()

    def failed(self, invocation="review-1", status="timeout", artifact=None):
        self.ledger.start_model_invocation(invocation_id=invocation, ticket_id=self.ticket, attempt_number=2, stage="review", provider="p", model="m", packet_hash="b" * 64, worktree_path="/tmp/attempt", timeout_seconds=60)
        self.ledger.finish_model_invocation(invocation, status=status, duration_seconds=1, error={"type": status}, model_artifact=artifact)

    def test_timeout_without_verdict_keeps_same_candidate_retryable(self):
        self.failed()
        status = self.ledger.review_reconciliation_status(self.ticket, 2)
        self.assertEqual(status["classification"], "review_invocation_failed_no_verdict")
        self.assertTrue(status["retry_eligible"])
        self.assertEqual(self.ledger.review_candidate(self.ticket, 2)["candidate_fingerprint"], self.fp)
        self.assertEqual(self.ledger.attempt_count(self.ticket), 0)

    def test_malformed_response_preserves_raw_provenance_without_verdict(self):
        raw = self.root / "malformed.json"; raw.write_text('{"content_type":"text","parsed":"no"}')
        self.failed(status="malformed_output", artifact=str(raw))
        status = self.ledger.review_reconciliation_status(self.ticket, 2)
        self.assertEqual(status["classification"], "review_invocation_failed_no_verdict")
        self.assertEqual(status["latest_invocation_status"], "malformed_output")
        self.assertEqual(status["latest_response_artifact"], str(raw))
        self.assertFalse(self.ledger.has_valid_review_verdict(self.ticket, 2))

    def test_process_failure_has_no_fabricated_artifact(self):
        self.failed(status="process_error")
        status = self.ledger.review_reconciliation_status(self.ticket, 2)
        self.assertIsNone(status["latest_response_artifact"])

    def test_valid_stage_is_substantive_and_blocks_retry(self):
        artifact = self.root / "review.json"; artifact.write_text('{"payload": {}}')
        self.failed(status="completed", artifact=str(artifact))
        self.ledger.record_model_stage(self.ticket, 2, "review", purpose="review", adapter="test", request_hash="b"*64, response_artifact=str(artifact), worktree_path="/tmp/attempt", base_sha="c"*40, diff_hash=self.fp)
        status = self.ledger.review_reconciliation_status(self.ticket, 2)
        self.assertEqual(status["classification"], "valid_review_stage_pending_application")
        self.assertFalse(status["retry_eligible"])

    def test_reconciliation_is_idempotent_and_bounded(self):
        self.failed()
        first = self.ledger.authorize_review_retry(self.ticket, operator_id="operator", candidate_fingerprint=self.fp)
        second = self.ledger.authorize_review_retry(self.ticket, operator_id="operator", candidate_fingerprint=self.fp)
        self.assertEqual(first["authorization_id"], second["authorization_id"])
        self.assertEqual(self.ledger.attempt_count(self.ticket), 0)

    def test_inflight_blocks_second_review_and_newer_candidate_blocks_stale_one(self):
        self.ledger.start_model_invocation(invocation_id="inflight", ticket_id=self.ticket, attempt_number=2, stage="review", provider="p", model="m", packet_hash="b"*64, worktree_path="/tmp/attempt", timeout_seconds=60)
        self.assertEqual(self.ledger.review_reconciliation_status(self.ticket, 2)["classification"], "review_in_flight")
        with self.assertRaises(PermissionError): self.ledger.authorize_review_retry(self.ticket, operator_id="operator", candidate_fingerprint=self.fp)
        self.ledger.finish_model_invocation("inflight", status="timeout", duration_seconds=1)
        self.ledger.freeze_review_candidate(self.ticket, 3, candidate_fingerprint="d"*64, validation_evidence="passed", implementation_invocation_id=None, runtime_identity={})
        with self.assertRaises(ValueError): self.ledger.authorize_review_retry(self.ticket, operator_id="operator", candidate_fingerprint=self.fp)


if __name__ == "__main__": unittest.main()
