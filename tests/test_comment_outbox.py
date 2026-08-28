from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


# These are intentionally handwritten historical fixtures.  They must not be
# built from Ledger's current schema, or a migration regression could hide.
_PRE_SLICE_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE tickets (
    id TEXT PRIMARY KEY, external_id TEXT UNIQUE, title TEXT NOT NULL,
    state TEXT NOT NULL, lease_owner TEXT, lease_expires_at INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, event_type TEXT NOT NULL, from_state TEXT,
    to_state TEXT, actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL
);
CREATE TABLE board_projections (
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    event_id INTEGER NOT NULL REFERENCES events(id), state TEXT NOT NULL,
    projected_at INTEGER NOT NULL, PRIMARY KEY(ticket_id, event_id)
);
CREATE TABLE runtime_bindings (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    repository_path TEXT NOT NULL, starting_sha TEXT NOT NULL,
    ownership_verified INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL REFERENCES tickets(id),
    attempt_number INTEGER NOT NULL, accepted_commit_sha TEXT,
    created_at INTEGER NOT NULL, UNIQUE(ticket_id, attempt_number)
);
CREATE TABLE model_stage_artifacts (
    ticket_id TEXT NOT NULL REFERENCES tickets(id), attempt_number INTEGER NOT NULL,
    stage TEXT NOT NULL, purpose TEXT NOT NULL, adapter TEXT NOT NULL,
    request_hash TEXT NOT NULL, response_artifact TEXT NOT NULL,
    worktree_path TEXT NOT NULL, base_sha TEXT NOT NULL, diff_hash TEXT NOT NULL,
    completed_at INTEGER NOT NULL, UNIQUE(ticket_id, attempt_number, stage)
);
CREATE TABLE accepted_evidence (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
    accepted_commit_sha TEXT NOT NULL, diff_summary TEXT NOT NULL,
    validation_summary TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""

_PARTIAL_OUTBOX_SCHEMA = _PRE_SLICE_SCHEMA + """
CREATE TABLE evidence_comment_outbox (
    operation_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
    event_id INTEGER NOT NULL, external_task_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL, status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,
    last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    delivered_at INTEGER,
    UNIQUE(ticket_id, event_id, operation_kind)
);
"""


class CommentOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = TemporaryDirectory()
        self.p = Path(self.t.name) / "l.db"
        self.l = Ledger(self.p)
        self.l.migrate()
        self.ticket = self.l.create_ticket(title="t", state=CanonicalState.READY_LOCAL)

    def tearDown(self) -> None:
        self.l.close()
        self.t.cleanup()

    @staticmethod
    def _rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
        return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    @staticmethod
    def _row_dict(connection: sqlite3.Connection, table: str) -> dict[str, object]:
        row = connection.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT 1").fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _seed_historical_database(path: Path, schema: str) -> None:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(schema)
        connection.execute("INSERT INTO tickets VALUES ('ticket-1','external-1','old ticket','ready_local',NULL,NULL,100,101)")
        connection.execute("INSERT INTO events VALUES (1,'ticket','ticket-1','state_transition','draft','ready_local','controller','actor','{}',102)")
        connection.execute("INSERT INTO board_projections VALUES ('ticket-1',1,'ready_local',103)")
        connection.execute("INSERT INTO runtime_bindings VALUES ('ticket-1','/repo','base-sha',1,104)")
        connection.execute("INSERT INTO attempts VALUES (1,'ticket-1',1,'accepted-sha',105)")
        connection.execute("INSERT INTO model_stage_artifacts VALUES ('ticket-1',1,'validation','verify','local','request','response','/repo','base','diff',106)")
        connection.execute("INSERT INTO accepted_evidence VALUES ('ticket-1','accepted-sha','diff','validation',107)")
        if "evidence_comment_outbox" in schema:
            connection.execute("INSERT INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,attempt_count,lease_owner,last_error,created_at,updated_at) VALUES ('op-1','ticket-1',1,'external-1','evidence_comment','key-1','payload','pending',0,NULL,NULL,108,109)")
        connection.commit()
        connection.close()

    def test_enqueue_persistent_idempotent_marker_and_redaction(self):
        a = self.l.enqueue_evidence_comment(self.ticket, 7, "password=secret-value " * 80)
        b = self.l.enqueue_evidence_comment(self.ticket, 7, "different")
        self.assertEqual(a["operation_id"], b["operation_id"])
        self.assertIn("<!-- local-first-comment:", a["payload"])
        self.assertLessEqual(len(a["payload"]), 1000)
        self.assertNotIn("secret-value", a["payload"])
        self.l.close()
        self.l = Ledger(self.p)
        self.l.migrate()
        self.assertEqual(self.l.comment_outbox(a["operation_id"])["status"], "pending")

    def test_claim_state_machine_owner_and_recovery(self):
        a = self.l.enqueue_evidence_comment(self.ticket, 7, "a")
        op = a["operation_id"]
        self.assertTrue(self.l.claim_comment(op, "one", lease_seconds=1, now=10))
        self.assertFalse(self.l.claim_comment(op, "two", lease_seconds=1, now=10))
        self.assertFalse(self.l.mark_comment_delivered(op, "two", now=10))
        self.assertEqual(self.l.recover_expired_comment_leases(now=12), [op])
        self.assertTrue(self.l.claim_comment(op, "two", lease_seconds=1, now=12))
        self.assertTrue(self.l.mark_comment_delivered(op, "two", now=12))
        self.assertTrue(self.l.mark_comment_delivered(op, "two", now=12))
        self.assertFalse(self.l.claim_comment(op, "one", lease_seconds=1, now=13))

    def test_retry_and_permanent_failure(self):
        op = self.l.enqueue_evidence_comment(self.ticket, 7, "a")["operation_id"]
        self.l.claim_comment(op, "one", lease_seconds=1, now=1)
        self.assertTrue(self.l.mark_comment_retryable(op, "one", "token=abc", now=2))
        self.assertTrue(self.l.claim_comment(op, "two", lease_seconds=1, now=2))
        self.assertTrue(self.l.mark_comment_permanently_failed(op, "two", "bad", now=3))
        self.assertFalse(self.l.claim_comment(op, "x", lease_seconds=1, now=4))
        self.assertEqual(self.l.comment_outbox(op)["attempt_count"], 2)

    def test_pending_to_delivering_to_delivered(self):
        op = self.l.enqueue_evidence_comment(self.ticket, 7, "evidence")["operation_id"]
        self.assertEqual(self.l.comment_outbox(op)["status"], "pending")
        self.assertTrue(self.l.claim_comment(op, "worker", lease_seconds=60, now=10))
        self.assertEqual(self.l.comment_outbox(op)["status"], "delivering")
        self.assertTrue(self.l.mark_comment_delivered(op, "worker", now=11))
        self.assertEqual(self.l.comment_outbox(op)["status"], "delivered")

    def test_pre_slice_database_migrates_and_preserves_existing_data(self):
        self.l.close()
        self.p.unlink()
        self._seed_historical_database(self.p, _PRE_SLICE_SCHEMA)
        before = sqlite3.connect(self.p)
        before.row_factory = sqlite3.Row
        snapshots = {table: self._row_dict(before, table) for table in ("tickets", "events", "board_projections", "runtime_bindings", "attempts", "model_stage_artifacts", "accepted_evidence")}
        before.close()
        migrated = Ledger(self.p)
        migrated.migrate()
        columns = {row["name"] for row in migrated.connection.execute("PRAGMA table_info(evidence_comment_outbox)")}
        self.assertEqual(columns, {"operation_id", "ticket_id", "event_id", "external_task_id", "operation_kind", "idempotency_key", "payload", "status", "attempt_count", "lease_owner", "lease_expires_at", "next_attempt_at", "last_error", "created_at", "updated_at", "delivered_at"})
        self.assertEqual(self._rows(migrated.connection, "tickets")[0][:8], tuple(snapshots["tickets"].values()))
        for table, row in snapshots.items():
            migrated_row = self._row_dict(migrated.connection, table)
            for name, value in row.items():
                self.assertEqual(migrated_row[name], value, f"{table}.{name}")
        migrated.connection.execute("INSERT INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES ('op-a','ticket-1',1,'x','evidence_comment','key-a','p','pending',1,1)")
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.connection.execute("INSERT INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES ('op-b','ticket-1',2,'x','evidence_comment','key-a','p','pending',1,1)")
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.connection.execute("INSERT INTO evidence_comment_outbox(operation_id,ticket_id,event_id,external_task_id,operation_kind,idempotency_key,payload,status,created_at,updated_at) VALUES ('op-c','ticket-1',1,'x','evidence_comment','key-c','p','pending',1,1)")
        migrated.close()
        reopened = Ledger(self.p)
        reopened.migrate()
        for table, row in snapshots.items():
            reopened_row = self._row_dict(reopened.connection, table)
            for name, value in row.items():
                self.assertEqual(reopened_row[name], value, f"{table}.{name}")
        reopened.close()
        self.l = Ledger(self.p)

    def test_partial_slice_database_adds_columns_and_preserves_record(self):
        self.l.close()
        self.p.unlink()
        self._seed_historical_database(self.p, _PARTIAL_OUTBOX_SCHEMA)
        migrated = Ledger(self.p)
        before = dict(migrated.connection.execute("SELECT * FROM evidence_comment_outbox WHERE operation_id='op-1'").fetchone())
        migrated.migrate()
        after = migrated.comment_outbox("op-1")
        for name, value in before.items():
            self.assertEqual(after[name], value, name)
        self.assertIsNone(after["lease_expires_at"])
        self.assertIsNone(after["next_attempt_at"])
        schema_after_first_migration = self._rows(migrated.connection, "sqlite_master")
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.connection.execute("INSERT INTO evidence_comment_outbox VALUES ('op-2','ticket-1',1,'external-1','evidence_comment','key-2','p','pending',0,NULL,NULL,NULL,NULL,110,110,NULL)")
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.connection.execute("INSERT INTO evidence_comment_outbox VALUES ('op-3','ticket-1',2,'external-1','evidence_comment','key-1','p','pending',0,NULL,NULL,NULL,NULL,110,110,NULL)")
        migrated.close()
        reopened = Ledger(self.p)
        reopened.migrate()
        self.assertEqual(self._rows(reopened.connection, "sqlite_master"), schema_after_first_migration)
        self.assertEqual(reopened.comment_outbox("op-1"), after)
        reopened.close()
        self.l = Ledger(self.p)


if __name__ == "__main__":
    unittest.main()
