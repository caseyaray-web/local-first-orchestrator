from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.git_adapter import GitWorktreeAdapter


class ExecutionRootSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-b", "main"), cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(("git", "config", "user.email", "fixture@example.invalid"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Fixture"), cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-m", "base"), cwd=self.repo, check=True, capture_output=True, text=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_runtime_roots_inside_canonical_are_rejected_before_output_creation(self) -> None:
        worktrees = self.repo / ".hermes" / "worktrees"
        artifacts = self.repo / ".hermes" / "artifacts"
        config = RuntimeConfig(self.repo, worktrees, artifacts, (self.repo,))
        with self.assertRaisesRegex(ValueError, "unsafe_worktree_root"):
            config.validate_execution_roots()
        self.assertFalse((self.repo / ".hermes").exists())

    def test_runtime_artifact_root_symlink_to_canonical_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        alias = outside / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        config = RuntimeConfig(self.repo, outside / "worktrees", alias / "artifacts", (self.repo,))
        with self.assertRaisesRegex(ValueError, "unsafe_artifact_root"):
            config.validate_execution_roots()

    def test_attempt_root_inside_canonical_is_refused_before_git_worktree_add(self) -> None:
        base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()
        adapter = GitWorktreeAdapter(self.repo, self.repo / ".hermes" / "worktrees")
        with self.assertRaisesRegex(Exception, "unsafe_worktree_root"):
            adapter.create_attempt("T-1", 1, base)
        self.assertFalse((self.repo / ".hermes").exists())


if __name__ == "__main__":
    unittest.main()
