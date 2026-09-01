from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.context_packet import ContextBudgetError, ContextPacketBuilder
from local_first_orchestrator.git_adapter import DirtyCheckoutError, GitWorktreeAdapter, IntegrationHeadConflictError
from local_first_orchestrator.local_qwen import LocalQwenAdapter
from local_first_orchestrator.readiness import ReadinessError, validate_ticket
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.validation import DeterministicValidator, ValidationError


class Phase2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.run_git("config", "user.name", "Fixture")
        (self.repo / "app.py").write_text("def classify(value):\n    return 'missing'\n", encoding="utf-8")
        (self.repo / "test_app.py").write_text("from app import classify\n\ndef test_classify():\n    assert classify(1) == 'ok'\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "-m", "fixture base")
        self.base = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.ticket = MicroTicket(
            ticket_id="T-1", objective="Return ok for known fixture values.", criterion_ids=("C-1",),
            primary_symbol="app.py::classify", allowed_files=("app.py", "test_app.py"),
            forbidden_changes=("No lockfiles.",), patch_budget=PatchBudget(max_files=2, max_changed_lines=30),
            verification=VerificationProfile(commands=(("python", "-m", "pytest", "-q"),), working_directory="."),
            risk="low", review_required=True, max_attempts=2, dependencies=(),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, text=True, capture_output=True)

    def test_readiness_rejects_vague_and_broad_ticket_and_accepts_bounded_contract(self) -> None:
        self.assertEqual(validate_ticket(self.ticket).ticket_id, "T-1")
        with self.assertRaises(ReadinessError):
            validate_ticket(MicroTicket(**{**self.ticket.__dict__, "objective": "Improve the code."}))
        with self.assertRaises(ReadinessError):
            validate_ticket(MicroTicket(**{**self.ticket.__dict__, "allowed_files": ("src/", "a.py", "b.py")}))

    def test_git_adapter_preserves_dirty_checkout_and_isolates_ticket_branch(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        dirty = self.repo / "untracked.txt"
        dirty.write_text("dirty", encoding="utf-8")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        self.assertEqual(dirty.read_text(encoding="utf-8"), "dirty")
        self.assertNotEqual(attempt.path, self.repo)
        self.assertEqual(self.run_git("branch", "--show-current", cwd=attempt.path).stdout.strip(), attempt.branch)
        (attempt.path / "app.py").write_text("def classify(value):\n    return 'ok'\n", encoding="utf-8")
        self.assertEqual(self.run_git("branch", "--show-current").stdout.strip(), "main")
        self.assertNotEqual(attempt.branch, "main")
        adapter.teardown(attempt)

    def test_context_manifest_is_reproducible_and_required_content_cannot_exceed_budget(self) -> None:
        builder = ContextPacketBuilder(target_tokens=100, max_tokens=140)
        first = builder.build(self.ticket, {"app.py": (self.repo / "app.py").read_text()}, repository_rules="No network.")
        second = builder.build(self.ticket, {"app.py": (self.repo / "app.py").read_text()}, repository_rules="No network.")
        self.assertEqual(first.manifest, second.manifest)
        self.assertIn("content_hash", first.manifest["sections"][0])
        with self.assertRaises(ContextBudgetError):
            ContextPacketBuilder(target_tokens=1, max_tokens=3).build(self.ticket, {"app.py": "x" * 100}, repository_rules="rules")

    def test_context_packet_persists_deterministic_packet_and_manifest_artifacts(self) -> None:
        builder = ContextPacketBuilder(target_tokens=100, max_tokens=140)
        packet = builder.build(self.ticket, {"app.py": (self.repo / "app.py").read_text()}, repository_rules="No network.")
        first = builder.write_artifacts(packet, artifact_root=self.root / "context-artifacts")
        second = builder.write_artifacts(packet, artifact_root=self.root / "context-artifacts")
        self.assertTrue(first.packet_path.exists())
        self.assertTrue(first.manifest_path.exists())
        self.assertEqual(first, second)
        self.assertEqual(first.packet_hash, hashlib.sha256(packet.text.encode()).hexdigest())
        self.assertEqual(first.manifest_hash, hashlib.sha256(json.dumps(packet.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(json.loads(first.manifest_path.read_text(encoding="utf-8")), packet.manifest)

    def test_context_packet_refuses_oversized_mandatory_content_before_writing_artifacts(self) -> None:
        builder = ContextPacketBuilder(target_tokens=1, max_tokens=3)
        artifact_root = self.root / "unsafe-context-artifacts"
        with self.assertRaises(ContextBudgetError):
            builder.build(self.ticket, {"app.py": "x" * 100}, repository_rules="rules")
        self.assertFalse(artifact_root.exists())

    def test_validator_rejects_base_mismatch_scope_and_redacts_secrets(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        (attempt.path / "outside.py").write_text("password=supersecret", encoding="utf-8")
        validator = DeterministicValidator(artifact_root=self.root / "artifacts", environment_allowlist=("PATH",), secret_patterns=("supersecret",))
        result = validator.validate(attempt.path, self.ticket, base_sha=self.base)
        self.assertFalse(result.passed)
        self.assertIn("outside.py", result.errors[0])
        self.assertNotIn("supersecret", result.compact_evidence)
        with self.assertRaises(ValidationError):
            validator.validate(attempt.path, self.ticket, base_sha="0" * 40)
        adapter.teardown(attempt)

    def test_validator_rejects_no_changes(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        result = DeterministicValidator(artifact_root=self.root / "artifacts").validate(attempt.path, self.ticket, base_sha=self.base)
        self.assertFalse(result.passed); self.assertIn("no_changes", result.errors[0])
        adapter.teardown(attempt)

    def test_validator_rejects_unexpected_head_movement(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        (attempt.path / "app.py").write_text("def classify(value):\n    return 'moved'\n", encoding="utf-8")
        self.run_git("add", "app.py", cwd=attempt.path); self.run_git("commit", "-m", "worker commit", cwd=attempt.path)
        with self.assertRaisesRegex(ValidationError, "unexpected_head_movement"):
            DeterministicValidator(artifact_root=self.root / "artifacts").validate(attempt.path, self.ticket, base_sha=self.base)
        adapter.teardown(attempt)

    def test_validator_only_runs_ticket_allowlisted_commands(self) -> None:
        profile = VerificationProfile(commands=(("python", "-c", "print('allowed')"),), working_directory=".")
        ticket = MicroTicket(**{**self.ticket.__dict__, "verification": profile})
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(ticket.ticket_id, 1, self.base)
        (attempt.path / "app.py").write_text("def classify(value):\n    return 'allowed'\n", encoding="utf-8")
        validator = DeterministicValidator(artifact_root=self.root / "artifacts")
        result = validator.validate(attempt.path, ticket, base_sha=self.base)
        self.assertTrue(result.passed)
        self.assertEqual(result.commands[0].argv, profile.commands[0])
        adapter.teardown(attempt)

    def test_validator_rejects_secret_in_allowed_changed_file_before_commands_and_redacts_evidence(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        secret = "actual-secret-value"
        (attempt.path / "app.py").write_text(f"credential = '{secret}'\n", encoding="utf-8")
        marker = attempt.path / "command-ran"
        profile = VerificationProfile(commands=(("python", "-c", "from pathlib import Path; Path('command-ran').write_text('yes')"),))
        ticket = MicroTicket(**{**self.ticket.__dict__, "verification": profile})
        validator = DeterministicValidator(artifact_root=self.root / "artifacts", secret_patterns=(secret,))
        result = validator.validate(attempt.path, ticket, base_sha=self.base)
        evidence = result.full_evidence_path.read_text(encoding="utf-8")
        self.assertFalse(result.passed)
        self.assertFalse(marker.exists())
        self.assertNotIn(secret, " ".join(result.errors))
        self.assertNotIn(secret, result.compact_evidence)
        self.assertNotIn(secret, evidence)
        adapter.teardown(attempt)

    def test_validator_rejects_conservative_default_secret_assignment_pattern(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        (attempt.path / "app.py").write_text("API_TOKEN = 'default-secret-value'\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "artifacts").validate(attempt.path, self.ticket, base_sha=self.base)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors, ("secret material detected in changed file: app.py",))
        self.assertNotIn("default-secret-value", result.full_evidence_path.read_text(encoding="utf-8"))
        adapter.teardown(attempt)

    def test_fake_qwen_runner_gets_pinned_agentic_workspace(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, dict(kwargs)))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"changed_files": ["app.py"]}), stderr="")
        attempt=self.root / "attempt"; attempt.mkdir()
        adapter = LocalQwenAdapter(runner=runner, hermes_home=self.root / "local-coder-home")
        result = adapter.invoke("implementation", "packet", artifact_dir=self.root / "model", workdir=attempt)
        self.assertEqual(len(calls), 1)
        argv, kwargs=calls[0]
        self.assertEqual(argv, ("hermes", "chat", "--toolsets", "file,terminal", "--in", str(attempt), "--provider", "custom:lm-studio", "--model", "qwen3.8-27b@iq3_s", "--query", "packet", "--quiet"))
        self.assertEqual(kwargs["cwd"],str(attempt))
        self.assertEqual(kwargs["env"]["HERMES_HOME"],str((self.root / "local-coder-home").resolve()))
        self.assertEqual(kwargs["env"]["TERMINAL_CWD"],str(attempt))
        self.assertNotIn("--fresh-session", argv)
        self.assertTrue(result.artifact_path.exists())
        self.assertEqual(result.payload["changed_files"], ["app.py"])

    def test_review_does_not_grant_implementation_tools(self) -> None:
        calls=[]
        class StructuredLlm:
            def complete_structured(self, **kwargs: object) -> object:
                calls.append(kwargs)
                return SimpleNamespace(parsed={"verdict":"pass","criterion_results":[],"findings":[],"suggestions":[]},content_type="json")
        LocalQwenAdapter(review_llm=StructuredLlm()).invoke("review", "packet", artifact_dir=self.root / "review", workdir=self.root)
        self.assertNotIn("workdir",calls[0])
        self.assertNotIn("tools",calls[0])

    def test_review_uses_host_structured_inference_with_local_qwen_and_no_cli_tool_loop(self) -> None:
        calls: list[dict[str, object]] = []

        class StructuredLlm:
            def complete_structured(self, **kwargs: object) -> object:
                calls.append(kwargs)
                return SimpleNamespace(
                    parsed={"verdict": "pass", "criterion_results": [], "findings": [], "suggestions": []},
                    content_type="json",
                )

        def no_cli(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.fail("review must not invoke the Hermes CLI or a tool loop")

        result = LocalQwenAdapter(runner=no_cli, review_llm=StructuredLlm()).invoke(
            "review", "packet", artifact_dir=self.root / "review-structured"
        )
        self.assertEqual(result.payload["verdict"], "pass")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["provider"], "custom:lm-studio")
        self.assertEqual(call["model"], "qwen3.8-27b@iq3_s")
        self.assertEqual(call["input"], [{"type": "text", "text": "packet"}])
        self.assertEqual(call["json_schema"]["additionalProperties"], False)
        self.assertEqual(call["json_schema"]["required"], ["verdict", "criterion_results", "findings", "suggestions"])
        self.assertNotIn("tools", call)

    def test_review_rejects_non_json_structured_response_without_text_extraction(self) -> None:
        class TextLlm:
            def complete_structured(self, **kwargs: object) -> object:
                return SimpleNamespace(parsed=None, content_type="text", text='{"verdict":"pass"}')

        with self.assertRaisesRegex(ValueError, "structured JSON"):
            LocalQwenAdapter(review_llm=TextLlm()).invoke("review", "packet", artifact_dir=self.root / "review-text")

    def test_implementation_allows_successful_hermes_text_while_review_remains_structured(self) -> None:
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="edited app.py", stderr="")
        class TextLlm:
            def complete_structured(self, **kwargs: object) -> object:
                return SimpleNamespace(parsed=None,content_type="text",text="edited app.py")
        adapter=LocalQwenAdapter(runner=runner,review_llm=TextLlm())
        result=adapter.invoke("implementation", "packet", artifact_dir=self.root / "model-text")
        self.assertEqual(result.payload, {})
        with self.assertRaisesRegex(ValueError, "structured JSON"):
            adapter.invoke("review", "packet", artifact_dir=self.root / "review-text")

    def test_tranche_integration_head_uses_compare_and_swap(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        self.assertEqual(adapter.resolve_execution_base("tranche-a", self.base), self.base)
        ref = "refs/local-first/tranches/tranche-a/integration-head"
        self.assertEqual(self.run_git("rev-parse", ref).stdout.strip(), self.base)
        attempt = adapter.create_attempt("T-1", 1, self.base)
        (attempt.path / "app.py").write_text("def classify(value):\n    return 'ok'\n", encoding="utf-8")
        accepted = adapter.accept(attempt, "fixture ticket accepted")
        self.assertEqual(adapter.advance_integration_head("tranche-a", self.base, accepted), accepted)
        self.assertEqual(adapter.resolve_execution_base("tranche-a", self.base), accepted)
        with self.assertRaisesRegex(IntegrationHeadConflictError, "integration head changed concurrently"):
            adapter.advance_integration_head("tranche-a", self.base, accepted)
        self.assertEqual(self.run_git("rev-parse", ref).stdout.strip(), accepted)

    def test_fixture_happy_path_commits_only_ticket_branch_and_rejects_scope(self) -> None:
        adapter = GitWorktreeAdapter(self.repo, self.root / "worktrees")
        attempt = adapter.create_attempt(self.ticket.ticket_id, 1, self.base)
        # This is the deterministic fake local-model edit; no model process is invoked.
        (attempt.path / "app.py").write_text("def classify(value):\n    return 'ok'\n", encoding="utf-8")
        ticket = MicroTicket(**{**self.ticket.__dict__, "verification": VerificationProfile(commands=(("python", "-c", "print('fixture validated')"),))})
        validator = DeterministicValidator(artifact_root=self.root / "artifacts")
        self.assertTrue(validator.validate(attempt.path, ticket, base_sha=self.base).passed)
        accepted = adapter.accept(attempt, "fixture ticket accepted")
        self.assertEqual(self.run_git("rev-parse", "HEAD", cwd=attempt.path).stdout.strip(), accepted)
        self.assertEqual(self.run_git("rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertNotEqual(self.run_git("branch", "--show-current", cwd=attempt.path).stdout.strip(), "main")
        adapter.teardown(attempt)


if __name__ == "__main__":
    unittest.main()
