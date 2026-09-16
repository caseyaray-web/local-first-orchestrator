from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.worktree_lifecycle import cleanup_completed_worktree


class WorktreeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "ticket-wt"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.email", "test@example.invalid"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Test"), cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(("git", "add", "app.py"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "base"), cwd=self.repo, check=True)
        subprocess.run(("git", "worktree", "add", "-q", "-b", "ticket", str(self.worktree), "HEAD"), cwd=self.repo, check=True)
        (self.worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(("git", "add", "app.py"), cwd=self.worktree, check=True)
        subprocess.run(("git", "commit", "-qm", "accepted"), cwd=self.worktree, check=True)
        self.accepted = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clean_accepted_worktree_is_removed(self) -> None:
        self.assertEqual(cleanup_completed_worktree(self.repo, self.worktree, accepted_commit_sha=self.accepted), "removed")
        self.assertFalse(self.worktree.exists())

    def test_dirty_worktree_is_retained(self) -> None:
        (self.worktree / "debug.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            cleanup_completed_worktree(self.repo, self.worktree, accepted_commit_sha=self.accepted)
        self.assertTrue(self.worktree.exists())

    def test_head_mismatch_is_retained(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "accepted commit"):
            cleanup_completed_worktree(self.repo, self.worktree, accepted_commit_sha="0" * 40)
        self.assertTrue(self.worktree.exists())

    def test_missing_worktree_registration_is_pruned(self) -> None:
        shutil.rmtree(self.worktree)
        self.assertEqual(cleanup_completed_worktree(self.repo, self.worktree, accepted_commit_sha=self.accepted), "pruned_missing")
        listing = subprocess.run(("git", "worktree", "list", "--porcelain"), cwd=self.repo, text=True, capture_output=True, check=True).stdout
        self.assertNotIn(str(self.worktree), listing)


if __name__ == "__main__":
    unittest.main()
