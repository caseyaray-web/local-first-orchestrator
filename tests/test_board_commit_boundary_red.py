import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger


class BoardCommitBoundaryTests(unittest.TestCase):
    def test_ledger_authorization_api_has_no_callable_freshness_path(self):
        import inspect
        signature = inspect.signature(Ledger.record_native_release_revalidation)
        self.assertNotIn("freshness_guard", signature.parameters)

    def test_adapter_revalidation_holds_board_write_lock_until_context_exit(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "board.db"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT, branch_name TEXT, session_id TEXT, current_run_id INTEGER); CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY(parent_id, child_id)); CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT, worker_pid INTEGER, metadata TEXT); INSERT INTO tasks VALUES ('T','title','body','impl','blocked',1,'test',1,NULL,NULL,'worktree','/tmp/wt',NULL,NULL,NULL,'main',NULL,NULL);")
            conn.commit(); conn.close()
            adapter = HermesBoardAdapter(board="default", executable="/bin/true", board_db_path=db)
            with adapter.revalidation("T") as capability:
                self.assertEqual(capability.snapshot.task.id, "T")
                competing = sqlite3.connect(db, timeout=0.05)
                with self.assertRaises(sqlite3.OperationalError):
                    competing.execute("UPDATE tasks SET status='running' WHERE id='T'")
                competing.close()

    def test_competing_write_waits_then_applies_after_commit(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "board.db"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT, branch_name TEXT, session_id TEXT, current_run_id INTEGER); CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY(parent_id, child_id)); CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT, worker_pid INTEGER, metadata TEXT); INSERT INTO tasks VALUES ('T','title','body','impl','blocked',1,'test',1,NULL,NULL,'worktree','/tmp/wt',NULL,NULL,NULL,'main',NULL,NULL);")
            conn.commit(); conn.close()
            adapter = HermesBoardAdapter(board="default", executable="/bin/true", board_db_path=db, timeout_seconds=2)
            started = threading.Event(); finished = threading.Event()
            def compete():
                other = sqlite3.connect(db, timeout=2.0, isolation_level=None)
                started.set()
                other.execute("UPDATE tasks SET status='running' WHERE id='T'")
                other.close(); finished.set()
            worker = threading.Thread(target=compete)
            with adapter.revalidation("T") as proof:
                worker.start()
                started.wait(1)
                self.assertFalse(finished.wait(0.1))
                proof._verify_for_ledger("T", "T")
            worker.join(3)
            self.assertTrue(finished.is_set())
            self.assertEqual(sqlite3.connect(db).execute("SELECT status FROM tasks WHERE id='T'").fetchone()[0], "running")

    def test_drift_on_locked_connection_is_rejected(self):
        with TemporaryDirectory() as temp:
            db = Path(temp) / "board.db"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT, branch_name TEXT, session_id TEXT, current_run_id INTEGER); CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY(parent_id, child_id)); CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT, worker_pid INTEGER, metadata TEXT); INSERT INTO tasks VALUES ('T','title','body','impl','blocked',1,'test',1,NULL,NULL,'worktree','/tmp/wt',NULL,NULL,NULL,'main',NULL,NULL);")
            conn.commit(); conn.close()
            adapter = HermesBoardAdapter(board="default", executable="/bin/true", board_db_path=db)
            with self.assertRaisesRegex(RuntimeError, "drift"):
                with adapter.revalidation("T") as proof:
                    proof._connection.execute("UPDATE tasks SET status='running' WHERE id='T'")
                    proof._verify_for_ledger("T", "T")


if __name__ == "__main__":
    unittest.main()
