from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.comment_delivery import CommentDeliveryPolicy, CommentDeliveryWorker
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class FakeCommentAdapter:
    def __init__(self, ledger: Ledger, failures: list[Exception] | None = None, writes_enabled: bool = True) -> None:
        self.ledger = ledger
        self.failures = list(failures or [])
        self.writes_enabled = writes_enabled
        self.calls: list[tuple[str, str, str, bool]] = []

    def deliver_comment(self, external_task_id: str, comment: str, *, idempotency_key: str) -> None:
        self.calls.append((external_task_id, comment, idempotency_key, self.ledger.connection.in_transaction))
        if self.failures:
            raise self.failures.pop(0)


class CommentDeliveryWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.db"
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.READY_LOCAL, external_id="external-42")
        self.operation = self.ledger.enqueue_evidence_comment(self.ticket, 7, "evidence")["operation_id"]

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def worker(self, adapter: FakeCommentAdapter, **kwargs: object) -> CommentDeliveryWorker:
        return CommentDeliveryWorker(self.ledger, adapter, worker_id="worker", clock=lambda: int(kwargs.pop("now", 100)), **kwargs)

    def test_no_work_result(self) -> None:
        adapter = FakeCommentAdapter(self.ledger)
        worker = self.worker(adapter)
        self.assertEqual(worker.deliver_one().status, "delivered")
        self.assertEqual(worker.deliver_one().status, "no_work")
        self.assertEqual(len(adapter.calls), 1)

    def test_writes_disabled_does_not_claim(self) -> None:
        adapter = FakeCommentAdapter(self.ledger, writes_enabled=False)
        result = self.worker(adapter).deliver_one()
        self.assertEqual(result.status, "writes_disabled")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["status"], "pending")
        self.assertEqual(adapter.calls, [])

    def test_success_passes_persisted_external_id_payload_and_marker(self) -> None:
        expected = self.ledger.comment_outbox(self.operation)
        adapter = FakeCommentAdapter(self.ledger)
        result = self.worker(adapter).deliver_one()
        self.assertEqual(result.status, "delivered")
        self.assertEqual(adapter.calls, [(expected["external_task_id"], expected["payload"], expected["idempotency_key"], False)])
        self.assertIn(f"<!-- local-first-comment:{self.operation} -->", adapter.calls[0][1])
        self.assertEqual(self.ledger.comment_outbox(self.operation)["attempt_count"], 1)

    def test_policy_write_disable_does_not_claim(self) -> None:
        adapter = FakeCommentAdapter(self.ledger)
        policy = CommentDeliveryPolicy(writes_enabled=False)
        result = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="worker", clock=lambda: 100).deliver_one()
        self.assertEqual(result.status, "writes_disabled")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["attempt_count"], 0)

    def test_first_failure_is_retryable_with_deterministic_backoff_and_redaction(self) -> None:
        adapter = FakeCommentAdapter(self.ledger, [RuntimeError("token=secret password=hunter2 " + "x" * 1000)])
        policy = CommentDeliveryPolicy(base_retry_delay=7, max_retry_delay=20, error_length_limit=40)
        result = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="worker", clock=lambda: 100).deliver_one()
        row = self.ledger.comment_outbox(self.operation)
        self.assertEqual(result.status, "retry_scheduled")
        self.assertEqual(result.next_attempt_at, 107)
        self.assertEqual(row["next_attempt_at"], 107)
        self.assertEqual(row["attempt_count"], 1)
        self.assertLessEqual(len(row["last_error"]), 40)
        self.assertNotIn("secret", row["last_error"])
        self.assertNotIn("hunter2", row["last_error"])

    def test_retry_is_not_claimable_before_due_and_succeeds_later(self) -> None:
        adapter = FakeCommentAdapter(self.ledger, [RuntimeError("nope")])
        policy = CommentDeliveryPolicy(base_retry_delay=10)
        first = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="worker", clock=lambda: 100).deliver_one()
        self.assertEqual(first.next_attempt_at, 110)
        early = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="other", clock=lambda: 109).deliver_one()
        self.assertEqual(early.status, "no_work")
        late = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="other", clock=lambda: 110).deliver_one()
        self.assertEqual(late.status, "delivered")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["attempt_count"], 2)
        self.assertEqual(len(adapter.calls), 2)

    def test_exact_attempt_count_and_permanent_failure_at_maximum(self) -> None:
        adapter = FakeCommentAdapter(self.ledger, [RuntimeError("failure")] * 3)
        policy = CommentDeliveryPolicy(max_attempts=3, base_retry_delay=1, max_retry_delay=1)
        for now in (1, 2):
            self.assertEqual(CommentDeliveryWorker(self.ledger, adapter, policy, worker_id=f"w{now}", clock=lambda now=now: now).deliver_one().status, "retry_scheduled")
        result = CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="w3", clock=lambda: 3).deliver_one()
        self.assertEqual(result.status, "permanently_failed")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["attempt_count"], 3)
        self.assertEqual(len(adapter.calls), 3)
        self.assertEqual(CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="w4", clock=lambda: 4).deliver_one().status, "no_work")

    def test_expired_lease_is_recovered_then_delivered(self) -> None:
        self.assertTrue(self.ledger.claim_comment(self.operation, "dead", lease_seconds=1, now=10))
        adapter = FakeCommentAdapter(self.ledger)
        result = CommentDeliveryWorker(self.ledger, adapter, worker_id="live", clock=lambda: 11).deliver_one()
        self.assertEqual(result.status, "delivered")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["attempt_count"], 2)

    def test_close_reopen_preserves_retry_before_later_delivery(self) -> None:
        adapter = FakeCommentAdapter(self.ledger, [RuntimeError("temporary")])
        policy = CommentDeliveryPolicy(base_retry_delay=5)
        self.assertEqual(CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="one", clock=lambda: 10).deliver_one().status, "retry_scheduled")
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        adapter.ledger = self.ledger
        self.assertEqual(CommentDeliveryWorker(self.ledger, adapter, policy, worker_id="two", clock=lambda: 15).deliver_one().status, "delivered")

    def test_adapter_never_called_for_terminal_operation(self) -> None:
        self.ledger.claim_comment(self.operation, "worker", now=1)
        self.ledger.mark_comment_permanently_failed(self.operation, "worker", "terminal", now=2)
        adapter = FakeCommentAdapter(self.ledger)
        self.assertEqual(self.worker(adapter).deliver_one().status, "no_work")
        self.assertEqual(adapter.calls, [])

    def test_two_workers_cannot_deliver_same_active_claim(self) -> None:
        adapter = FakeCommentAdapter(self.ledger)
        first = CommentDeliveryWorker(self.ledger, adapter, worker_id="first", clock=lambda: 100)
        second = CommentDeliveryWorker(self.ledger, adapter, worker_id="second", clock=lambda: 100)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def run(worker: CommentDeliveryWorker) -> None:
            barrier.wait()
            results.append(worker.deliver_one().status)

        threads = [threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(results), ["delivered", "no_work"])
        self.assertEqual(len(adapter.calls), 1)

    def test_comment_failure_does_not_change_delivered_state_projection(self) -> None:
        self.ledger.claim_comment(self.operation, "setup", now=1)
        self.ledger.mark_comment_delivered(self.operation, "setup", now=2)
        ticket = self.ledger.create_ticket(title="projected", state=CanonicalState.DRAFT, external_id="projected-external")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        operation = self.ledger.connection.execute("SELECT operation_id FROM evidence_comment_outbox WHERE ticket_id=?", (ticket,)).fetchone()[0]
        state_outbox = self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone()
        self.ledger.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=200 WHERE ticket_id=?", (ticket,))
        adapter = FakeCommentAdapter(self.ledger, [RuntimeError("comment only")])
        result = CommentDeliveryWorker(self.ledger, adapter, worker_id="worker", clock=lambda: 100).deliver_one()
        self.assertEqual(result.status, "retry_scheduled")
        self.assertEqual(self.ledger.connection.execute("SELECT acknowledged_at FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone()[0], 200)
        self.assertEqual(self.ledger.comment_outbox(operation)["status"], "retryable")
        self.assertIsNotNone(state_outbox)

    def test_comment_success_does_not_depend_on_state_projection(self) -> None:
        adapter = FakeCommentAdapter(self.ledger)
        result = CommentDeliveryWorker(self.ledger, adapter, worker_id="worker", clock=lambda: 100).deliver_one()
        self.assertEqual(result.status, "delivered")
        self.assertEqual(self.ledger.comment_outbox(self.operation)["status"], "delivered")
        self.assertEqual(self.ledger.connection.execute("SELECT acknowledged_at FROM board_projection_outbox").fetchone(), None)
        self.assertEqual(len(adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
