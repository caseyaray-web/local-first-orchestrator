from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.adapters import FakeBoardAdapter
from local_first_orchestrator.execution_handoff import HANDOFF_MARKER
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.state_projection import StateProjectionWorker
from local_first_orchestrator.states import CanonicalState


class PrematureDoneBoard(FakeBoardAdapter):
    def __init__(self, external_id: str) -> None:
        super().__init__()
        self.external_id = external_id
        self.set_state_calls = 0

    def get_task(self, ticket_id: str) -> ExternalTicket:
        if ticket_id != self.external_id:
            raise KeyError(ticket_id)
        return ExternalTicket(ticket_id, "generated", HANDOFF_MARKER, "done", None)

    def set_state(self, ticket_id, state, *, idempotency_key):
        self.set_state_calls += 1
        raise AssertionError("intermediate generated-done projection must be acknowledged without board mutation")


class StateProjectionSupersessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ledger.db"
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        self.board = FakeBoardAdapter()

    def tearDown(self) -> None:
        self.ledger.close()
        self.tempdir.cleanup()

    def _history_to_local_review(self, *, external_id: str = "external-42") -> str:
        ticket = self.ledger.create_ticket(title="projection recovery", external_id=external_id)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.BLOCKED)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket, CanonicalState.NEEDS_TRIAGE)
        self.ledger.transition(ticket, CanonicalState.LOCAL_REVIEW)
        return ticket

    def _state_rows(self, ticket: str):
        return self.ledger.connection.execute(
            "SELECT event_id,state,superseded_at,superseded_by_event_id FROM board_projection_outbox "
            "WHERE ticket_id=? AND operation='set_state' ORDER BY event_id",
            (ticket,),
        ).fetchall()

    def test_reconciled_generated_done_handoff_acknowledges_intermediate_projection_without_board_mutation(self) -> None:
        ticket = self.ledger.create_ticket(title="generated done handoff")
        with self.ledger._transaction() as conn:
            create_event = self.ledger._append_event(conn, entity_type="ticket", entity_id=ticket, event_type="generated_microticket_created", actor_id="controller", to_state="draft", payload={"ticket_id": ticket})
        self.ledger.enqueue_generated_create_projection(ticket, create_event, {"ticket_id": ticket}, "create-generated")
        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET acknowledged_at=10,external_task_id='external-generated' WHERE ticket_id=? AND event_id=? AND operation='create_microticket'",
            (ticket, create_event),
        )
        self.ledger.connection.execute(
            "INSERT INTO hermes_execution_reconciliations(external_task_id,hermes_run_id,ticket_id,attempt_number,run_status,run_outcome,session_id,branch_name,workspace_path,base_sha,head_sha,diff_hash,artifact_path,snapshot_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("external-generated", 7, ticket, 1, "done", "completed", None, "wt/external-generated", "/tmp/worktree", "a" * 40, "b" * 40, "c" * 64, "/tmp/artifact", "d" * 64, 20),
        )
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket, CanonicalState.VERIFYING)
        self.ledger.transition(ticket, CanonicalState.LOCAL_REVIEW)
        board = PrematureDoneBoard("external-generated")
        worker = StateProjectionWorker(self.ledger, board, worker_id="generated-done")

        result = worker.deliver_one(now=30)

        self.assertEqual((result.status, result.ticket_id), ("delivered", ticket))
        self.assertEqual(board.set_state_calls, 0)
        row = self.ledger.connection.execute(
            "SELECT acknowledged_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket, result.event_id),
        ).fetchone()
        self.assertIsNotNone(row["acknowledged_at"])
        audit = self.ledger.runtime_stage(ticket, f"state_projection_generated_done_noop-{result.event_id}")
        self.assertIsNotNone(audit)

    def test_pending_history_is_superseded_and_only_current_state_is_delivered(self) -> None:
        ticket = self._history_to_local_review()
        rows = self._state_rows(ticket)
        self.assertEqual([row["state"] for row in rows], ["ready_local", "blocked", "ready_local", "needs_triage", "local_review"])
        self.assertTrue(all(row["superseded_at"] is not None for row in rows[:-1]))
        self.assertTrue(all(row["superseded_by_event_id"] is not None and row["superseded_by_event_id"] > row["event_id"] for row in rows[:-1]))
        self.assertIsNone(rows[-1]["superseded_at"])

        worker = StateProjectionWorker(self.ledger, self.board, worker_id="test")
        self.assertEqual(worker.deliver_one().status, "delivered")
        self.assertEqual(self.board.states, {"external-42": CanonicalState.LOCAL_REVIEW})
        self.assertEqual(worker.deliver_one().status, "no_work")

    def test_claimed_stale_row_is_not_sent_after_newer_transition(self) -> None:
        ticket = self.ledger.create_ticket(title="race", external_id="external-42")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket, CanonicalState.NEEDS_TRIAGE)
        old_event = self.ledger.events_for(ticket)[-1]["id"]
        self.assertTrue(self.ledger.claim_state_projection(ticket, old_event, "old-worker", lease_seconds=60, now=100))
        self.ledger.transition(ticket, CanonicalState.LOCAL_REVIEW)

        worker = StateProjectionWorker(self.ledger, self.board, worker_id="old-worker")
        self.assertEqual(worker.deliver_claimed(ticket, old_event, now=101).status, "superseded")
        self.assertEqual(self.board.projections, [])
        row = self.ledger.connection.execute(
            "SELECT superseded_by_event_id FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket, old_event),
        ).fetchone()
        self.assertIsNotNone(row["superseded_by_event_id"])

    def test_acknowledged_historical_projection_is_preserved_and_newer_state_corrects_it(self) -> None:
        ticket = self.ledger.create_ticket(title="delivered", external_id="external-42")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.IMPLEMENTING)
        self.ledger.transition(ticket, CanonicalState.NEEDS_TRIAGE)
        worker = StateProjectionWorker(self.ledger, self.board, worker_id="worker")
        self.assertEqual(worker.deliver_one().status, "delivered")
        old_event = self.ledger.events_for(ticket)[-1]["id"]
        self.ledger.transition(ticket, CanonicalState.LOCAL_REVIEW)
        old = self.ledger.connection.execute("SELECT acknowledged_at,superseded_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket, old_event)).fetchone()
        self.assertIsNotNone(old["acknowledged_at"])
        self.assertIsNone(old["superseded_at"])
        self.assertEqual(worker.deliver_one().status, "delivered")
        self.assertEqual(self.board.states, {"external-42": CanonicalState.LOCAL_REVIEW})

    def test_restart_keeps_superseded_history_non_deliverable_and_current_retryable(self) -> None:
        ticket = self._history_to_local_review()
        current = self._state_rows(ticket)[-1]
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        rows = self._state_rows(ticket)
        self.assertTrue(all(row["superseded_at"] is not None for row in rows[:-1]))
        self.assertIsNone(rows[-1]["superseded_at"])
        worker = StateProjectionWorker(self.ledger, self.board, worker_id="restart")
        self.assertEqual(worker.deliver_one().event_id, current["event_id"])

    def test_crash_after_adapter_success_replays_idempotently_after_lease_expiry(self) -> None:
        ticket = self.ledger.create_ticket(title="ambiguous state", external_id="external-42")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = int(self.ledger.events_for(ticket)[-1]["id"])

        crashing = StateProjectionWorker(
            self.ledger,
            self.board,
            worker_id="crasher",
            lease_seconds=2,
            fault_injector=lambda stage: (_ for _ in ()).throw(RuntimeError("simulated death")) if stage == "after_adapter_success" else None,
        )
        with self.assertRaisesRegex(RuntimeError, "simulated death"):
            crashing.deliver_one(now=100)

        row = self.ledger.connection.execute(
            "SELECT acknowledged_at,lease_owner,lease_expires_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket,event_id),
        ).fetchone()
        self.assertIsNone(row["acknowledged_at"])
        self.assertEqual(row["lease_owner"], "crasher")
        self.assertEqual(self.board.states["external-42"], CanonicalState.READY_LOCAL)

        restarted = StateProjectionWorker(self.ledger, self.board, worker_id="restart", lease_seconds=2)
        self.assertEqual(restarted.deliver_one(now=101).status, "no_work")
        result = restarted.deliver_one(now=103)
        self.assertEqual((result.status,result.ticket_id,result.event_id), ("delivered",ticket,event_id))
        final = self.ledger.connection.execute(
            "SELECT acknowledged_at,lease_owner FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket,event_id),
        ).fetchone()
        self.assertIsNotNone(final["acknowledged_at"])
        self.assertIsNone(final["lease_owner"])
        self.assertEqual(self.board.states["external-42"], CanonicalState.READY_LOCAL)

    def test_reconciliation_repairs_legacy_pending_rows_and_is_idempotent_without_mutating_events(self) -> None:
        ticket = self._history_to_local_review()
        self.ledger.connection.execute("UPDATE board_projection_outbox SET superseded_at=NULL,superseded_by_event_id=NULL,supersession_reason=NULL WHERE ticket_id=?", (ticket,))
        before_events = [(row["id"], row["to_state"]) for row in self.ledger.events_for(ticket)]
        first = self.ledger.reconcile_state_projections(ticket)
        second = self.ledger.reconcile_state_projections(ticket)
        self.assertEqual(first["current_projection_event_id"], second["current_projection_event_id"])
        self.assertEqual(second["superseded_count"], 0)
        self.assertEqual(before_events, [(row["id"], row["to_state"]) for row in self.ledger.events_for(ticket)])


if __name__ == "__main__":
    unittest.main()
