import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.hermes_board import HermesBoardAdapter


_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
    priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER,
    completed_at INTEGER, workspace_kind TEXT, workspace_path TEXT,
    claim_lock TEXT, claim_expires INTEGER, tenant TEXT, branch_name TEXT,
    session_id TEXT, current_run_id INTEGER
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY(parent_id, child_id));
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
    outcome TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT,
    worker_pid INTEGER, metadata TEXT
);
"""


class TerraRevalidationRegressionTests(unittest.TestCase):
    def _board(self, directory: Path, name: str = "board.db") -> Path:
        path = directory / name
        connection = sqlite3.connect(path)
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("EXT-1", "title", "body", "impl", "blocked", 1, "test", 1,
             None, None, "worktree", "/tmp/wt", None, None, None, "main", None, None),
        )
        connection.commit()
        connection.close()
        return path

    def _adapter(self, path: Path) -> HermesBoardAdapter:
        return HermesBoardAdapter(board="default", executable="/bin/true", board_db_path=path)

    def test_wrong_local_ticket_id_fails_before_capability_consumption(self) -> None:
        with TemporaryDirectory() as temp:
            path = self._board(Path(temp))
            with self._adapter(path).revalidation("LOCAL-1", "EXT-1") as capability:
                with self.assertRaisesRegex(PermissionError, "authority mismatch"):
                    capability._verify_for_ledger("LOCAL-2", "EXT-1")
                with self.assertRaisesRegex(PermissionError, "inactive or unregistered"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")

    def test_wrong_external_ticket_id_fails_before_capability_consumption(self) -> None:
        with TemporaryDirectory() as temp:
            path = self._board(Path(temp))
            with self._adapter(path).revalidation("LOCAL-1", "EXT-1") as capability:
                with self.assertRaisesRegex(PermissionError, "authority mismatch"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-2")
                with self.assertRaisesRegex(PermissionError, "inactive or unregistered"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")

    def test_replacement_after_context_entry_fails_identity_validation(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self._board(directory)
            replacement = self._board(directory, "replacement.db")
            adapter = self._adapter(path)
            with adapter.revalidation("LOCAL-1", "EXT-1") as capability:
                os.replace(replacement, path)
                with self.assertRaisesRegex(PermissionError, "identity"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")
                with self.assertRaisesRegex(PermissionError, "inactive or unregistered"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")

    def test_missing_configured_path_fails_identity_validation(self) -> None:
        with TemporaryDirectory() as temp:
            path = self._board(Path(temp))
            adapter = self._adapter(path)
            with adapter.revalidation("LOCAL-1", "EXT-1") as capability:
                path.unlink()
                with self.assertRaisesRegex(PermissionError, "identity"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")

    def test_symlink_retarget_fails_identity_validation(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            target = self._board(directory, "target.db")
            other = self._board(directory, "other.db")
            link = directory / "configured.db"
            link.symlink_to(target)
            adapter = self._adapter(link)
            with adapter.revalidation("LOCAL-1", "EXT-1") as capability:
                link.unlink()
                link.symlink_to(other)
                with self.assertRaisesRegex(PermissionError, "identity"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")

    def test_replacement_at_final_validation_point_fails_before_consume(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self._board(directory)
            replacement = self._board(directory, "replacement.db")
            adapter = self._adapter(path)
            with adapter.revalidation("LOCAL-1", "EXT-1") as capability:
                os.replace(replacement, path)
                with self.assertRaisesRegex(PermissionError, "identity"):
                    capability._verify_for_ledger("LOCAL-1", "EXT-1")


if __name__ == "__main__":
    unittest.main()
