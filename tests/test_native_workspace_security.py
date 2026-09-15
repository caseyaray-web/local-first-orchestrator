from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.native_workspace import PinnedNativeWorkspace, canonical_native_workspace_path


class NativeWorkspaceAdministrativePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q", str(self.repo)), check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(("git", "-C", str(self.repo), "add", "README"), check=True)
        subprocess.run(("git", "-C", str(self.repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), check=True)
        self.target = canonical_native_workspace_path(self.repo, "TASK-1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add_worktree(self, pin: PinnedNativeWorkspace) -> None:
        pin.git("worktree", "add", "-q", "-b", "wt/TASK-1", "TASK-1", "HEAD", creates_target=True, cwd_fd_override=pin.worktree_parent_fd)

    def test_parent_byte_copy_swap_after_git_add_stops_before_target_pin(self) -> None:
        original_run = subprocess.run
        swapped = False
        original_parent = self.repo / ".worktrees-original"

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            result = original_run(*args, **kwargs)
            if not swapped and "worktree" in command and "add" in command:
                swapped = True
                (self.repo / ".git" / "worktrees").rename(original_parent)
                shutil.copytree(original_parent, self.repo / ".git" / "worktrees")
            return result

        with PinnedNativeWorkspace.open(self.repo, self.target) as pin:
            with patch("local_first_orchestrator.native_workspace.subprocess.run", side_effect=swapping_run):
                with self.assertRaisesRegex(RuntimeError, "identity|STOP"):
                    self._add_worktree(pin)
        self.assertTrue(swapped)

    def test_child_byte_copy_swap_before_target_git_fd_stops(self) -> None:
        with PinnedNativeWorkspace.open(self.repo, self.target) as pin:
            self._add_worktree(pin)
            child = self.repo / ".git" / "worktrees" / "TASK-1"
            replacement = self.root / "metadata-replacement"
            shutil.copytree(child, replacement)
            shutil.rmtree(child)
            replacement.rename(child)
            with self.assertRaisesRegex(RuntimeError, "identity|STOP"):
                pin.pin_existing_target(already_created=True)

    def test_metadata_pin_survives_canonical_path_reads_without_reopening_it(self) -> None:
        with PinnedNativeWorkspace.open(self.repo, self.target) as pin:
            self._add_worktree(pin)
            pin.pin_existing_target(already_created=True)
            metadata_fd = pin.target_git_fd()
            try:
                self.assertIsNotNone(pin.target_git_metadata_identity)
                self.assertEqual(pin.target_git_metadata_id, "TASK-1")
                self.assertEqual(pin.target_git_metadata_fd, metadata_fd)
            finally:
                pin.close_target_git_metadata()


if __name__ == "__main__":
    unittest.main()
