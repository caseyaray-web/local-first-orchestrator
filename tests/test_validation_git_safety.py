from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.validation import DeterministicValidator
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


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

    def test_untracked_nested_symlink_is_not_hidden_by_directory_summary(self):
        with TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.email", "test@example.invalid"), cwd=repo, check=True)
            subprocess.run(("git", "config", "user.name", "Test"), cwd=repo, check=True)
            (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            subprocess.run(("git", "add", "app.py"), cwd=repo, check=True)
            subprocess.run(("git", "commit", "-qm", "base"), cwd=repo, check=True)
            base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
            (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            (repo / "hidden").mkdir()
            (repo / "hidden" / "link").symlink_to("/etc/passwd")
            ticket = MicroTicket(
                "T-symlink", "Change the bounded fixture value.", ("AC-1",), "app.py::value", ("app.py",),
                ("No unrelated files.",), PatchBudget(1, 20), VerificationProfile((("python", "-c", "print(1)"),)),
                "low", True, 1, (),
            )
            result = DeterministicValidator(artifact_root=Path(temp) / "artifacts").validate(repo, ticket, base_sha=base)
            self.assertFalse(result.passed)
            self.assertIn("changed path outside allowlist: hidden/link", result.errors)
            evidence = result.full_evidence_path.read_text(encoding="utf-8")
            self.assertIn('"hidden/link"', evidence)


if __name__ == "__main__": unittest.main()
