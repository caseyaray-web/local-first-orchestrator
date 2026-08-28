import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class AtomicBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.db"
        self.ledger = Ledger(self.path)
        self.ledger.migrate()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_successful_bundle_persists_and_reopens(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL, payload={"source": "test"})
        event = self.ledger.events_for(ticket)[0]
        bundle = self.ledger.enqueue_projection_bundle(ticket, event["id"], "state=ready_local", state_payload={"source": "test"})
        self.assertEqual(bundle["state"]["event_id"], event["id"])
        self.assertEqual(bundle["comment"]["event_id"], event["id"])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM evidence_comment_outbox").fetchone()[0], 1)

    def test_replay_is_idempotent_and_conflict_fails_closed(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = self.ledger.events_for(ticket)[0]["id"]
        first = self.ledger.enqueue_projection_bundle(ticket, event_id, "state=ready_local")
        second = self.ledger.enqueue_projection_bundle(ticket, event_id, "state=ready_local")
        self.assertEqual(first["state"]["operation_id"] if "operation_id" in first["state"] else first["state"]["idempotency_key"], second["state"]["idempotency_key"])
        with self.assertRaises(ValueError):
            self.ledger.enqueue_projection_bundle(ticket, event_id, "different evidence")

    def test_failure_after_event_state_or_comment_rolls_back_everything(self):
        for point in ("after_event_creation", "after_state_intent", "after_comment_intent"):
            self.ledger.close()
            self.ledger = Ledger(self.path, failure_injector=lambda actual, expected=point: (_ for _ in ()).throw(RuntimeError(actual)) if actual == expected else None)
            self.ledger.migrate()
            ticket = self.ledger.create_ticket(title=point, state=CanonicalState.DRAFT)
            before = self.ledger.connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            with self.assertRaisesRegex(RuntimeError, point):
                self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
            self.ledger.close()
            self.ledger = Ledger(self.path)
            self.ledger.migrate()
            self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events WHERE entity_id=?", (ticket,)).fetchone()[0], 0)
            self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone()[0], 0)
            self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM evidence_comment_outbox WHERE ticket_id=?", (ticket,)).fetchone()[0], 0)
            self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], before)
            self.assertEqual(self.ledger.get_ticket(ticket)["state"], CanonicalState.DRAFT.value)

    def test_terminal_status_and_payload_are_not_reset(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = self.ledger.events_for(ticket)[0]["id"]
        bundle = self.ledger.enqueue_projection_bundle(ticket, event_id, "state=ready_local")
        self.ledger.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=77 WHERE ticket_id=? AND event_id=?", (ticket, event_id))
        self.ledger.connection.execute("UPDATE evidence_comment_outbox SET status='permanently_failed', last_error='kept', terminal_owner='worker' WHERE operation_id=?", (bundle["comment"]["operation_id"],))
        self.ledger.enqueue_projection_bundle(ticket, event_id, "state=ready_local")
        state = self.ledger.connection.execute("SELECT acknowledged_at FROM board_projection_outbox WHERE ticket_id=? AND event_id=?", (ticket, event_id)).fetchone()[0]
        comment = self.ledger.comment_outbox(bundle["comment"]["operation_id"])
        self.assertEqual(state, 77)
        self.assertEqual(comment["status"], "permanently_failed")
        self.assertEqual(comment["last_error"], "kept")

    def test_internal_transition_has_no_bundle_and_planning_calls_no_adapter(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.READY_LOCAL)
        self.ledger.transition(ticket, CanonicalState.IMPLEMENTING)
        self.assertEqual(self.ledger.plan_projection(ticket), None)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM board_projection_outbox").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM evidence_comment_outbox").fetchone()[0], 0)

    def test_two_connections_resolve_same_bundle(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = self.ledger.events_for(ticket)[0]["id"]
        self.ledger.close()
        first, second = Ledger(self.path), Ledger(self.path)
        first.migrate(); second.migrate()
        barrier = threading.Barrier(2); results = []; errors = []
        def run(db):
            try:
                barrier.wait(timeout=2)
                results.append(db.enqueue_projection_bundle(ticket, event_id, "state=ready_local"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(db,)) for db in (first, second)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["state"]["idempotency_key"] for r in results}, {f"ticket-event:{event_id}"})
        self.assertEqual(len({r["comment"]["operation_id"] for r in results}), 1)
        self.assertEqual(first.connection.execute("SELECT COUNT(*) FROM board_projection_outbox").fetchone()[0], 1)
        self.assertEqual(first.connection.execute("SELECT COUNT(*) FROM evidence_comment_outbox").fetchone()[0], 1)
        first.close(); second.close()
        self.ledger = Ledger(self.path); self.ledger.migrate()

    def test_unmatched_legacy_intent_is_read_only_detectable(self):
        ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DRAFT)
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        event_id = self.ledger.events_for(ticket)[0]["id"]
        self.ledger.connection.execute("DELETE FROM evidence_comment_outbox WHERE ticket_id=? AND event_id=?", (ticket, event_id))
        report = self.ledger.projection_reconciliation_report()
        self.assertTrue(any(r["problem"] == "state_without_comment" for r in report))
        self.assertTrue(any(r["problem"] == "projectable_event_without_bundle" for r in report))


if __name__ == "__main__":
    unittest.main()
