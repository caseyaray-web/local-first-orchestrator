from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.comment_delivery import CommentDeliveryPolicy, CommentDeliveryWorker
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.state_projection import StateProjectionWorker
from local_first_orchestrator.states import CanonicalState


class ExternalBoundaryCrashHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_real_hermes_state_adapter_recovers_transport_loss_after_remote_state_change(self) -> None:
        remote = {"status": "todo"}
        writes: list[str] = []
        fail_after_schedule = {"value": True}

        def runner(argv, **kwargs):
            args = list(argv)
            command = args[4]
            if command == "show":
                payload = {
                    "task": {
                        "id": "external-1",
                        "title": "fixture",
                        "body": "",
                        "status": remote["status"],
                        "workspace_path": None,
                    },
                    "parents": [],
                    "children": [],
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if command == "schedule":
                writes.append("schedule")
                remote["status"] = "scheduled"
                if fail_after_schedule["value"]:
                    fail_after_schedule["value"] = False
                    raise OSError("transport lost after remote schedule")
                return subprocess.CompletedProcess(argv, 0, "scheduled", "")
            raise AssertionError(args)

        board = HermesBoardAdapter(
            executable="/bin/true",
            board="board",
            allow_writes=True,
            runner=runner,
        )
        ticket = self.ledger.create_ticket(title="state-boundary", external_id="external-1")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = int(self.ledger.events_for(ticket)[-1]["id"])

        first = StateProjectionWorker(self.ledger, board, worker_id="state-a", lease_seconds=30)
        with self.assertRaisesRegex(RuntimeError, "read unavailable"):
            first.deliver_one(now=100)
        self.assertEqual(remote["status"], "scheduled")
        self.assertEqual(writes, ["schedule"])
        pending = self.ledger.connection.execute(
            "SELECT acknowledged_at,last_error FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket, event_id),
        ).fetchone()
        self.assertIsNone(pending["acknowledged_at"])
        self.assertIn("read unavailable", pending["last_error"])

        resumed = StateProjectionWorker(self.ledger, board, worker_id="state-b", lease_seconds=30)
        result = resumed.deliver_one(now=101)
        self.assertEqual((result.status, result.ticket_id, result.event_id), ("delivered", ticket, event_id))
        self.assertEqual(writes, ["schedule"])

    def test_real_hermes_comment_adapter_reconciles_transport_loss_after_remote_append(self) -> None:
        comments: list[str] = []
        writes = {"count": 0}
        fail_after_comment = {"value": True}

        def runner(argv, **kwargs):
            args = list(argv)
            command = args[4]
            if command == "show":
                payload = {
                    "task": {
                        "id": "external-1",
                        "title": "fixture",
                        "body": "",
                        "status": "scheduled",
                        "workspace_path": None,
                    },
                    "parents": [],
                    "children": [],
                    "comments": [{"body": body} for body in comments],
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if command == "comment":
                writes["count"] += 1
                comments.append(args[6])
                if fail_after_comment["value"]:
                    fail_after_comment["value"] = False
                    raise OSError("transport lost after remote comment")
                return subprocess.CompletedProcess(argv, 0, "commented", "")
            raise AssertionError(args)

        board = HermesBoardAdapter(
            executable="/bin/true",
            board="board",
            allow_writes=True,
            runner=runner,
        )
        ticket = self.ledger.create_ticket(title="comment-boundary", external_id="external-1")
        operation = self.ledger.enqueue_evidence_comment(ticket, 1, "evidence")
        policy = CommentDeliveryPolicy(lease_seconds=30, max_attempts=3, base_retry_delay=1)

        first = CommentDeliveryWorker(self.ledger, board, policy, worker_id="comment-a", clock=lambda: 100)
        result = first.deliver_one()
        self.assertEqual(result.status, "retry_scheduled")
        self.assertEqual(writes["count"], 1)
        self.assertEqual(len(comments), 1)

        resumed = CommentDeliveryWorker(self.ledger, board, policy, worker_id="comment-b", clock=lambda: 101)
        result = resumed.deliver_one()
        self.assertEqual(result.status, "reconciled_delivered")
        self.assertEqual(result.operation_id, operation["operation_id"])
        self.assertEqual(writes["count"], 1)
        self.assertEqual(len(comments), 1)


if __name__ == "__main__":
    unittest.main()
