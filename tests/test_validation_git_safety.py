from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.validation import DeterministicValidator


class SafeGitInvocationTests(unittest.TestCase):
    def test_internal_git_disables_interactive_and_external_diff_paths(self):
        with TemporaryDirectory() as temp:
            calls=[]
            def fake_run(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, "", "")
            with patch("local_first_orchestrator.validation.subprocess.run", fake_run):
                DeterministicValidator(artifact_root=Path(temp))._git(Path(temp), "diff", "--numstat", "a" * 40, "--")
            argv, kwargs=calls[0]
            self.assertEqual(argv[:5], ("git", "--no-pager", "-c", "core.pager=cat", "-c"))
            self.assertIn("diff.external=false", argv)
            self.assertIn("--no-ext-diff", argv)
            self.assertIn("--no-textconv", argv)
            self.assertEqual(argv[-1], "--")
            self.assertEqual(kwargs["timeout"], 15)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__": unittest.main()
