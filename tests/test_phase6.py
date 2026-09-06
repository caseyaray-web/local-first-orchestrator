from __future__ import annotations

import hashlib
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from local_first_orchestrator.context_packet import ContextBudgetError, ContextPacketBuilder
from local_first_orchestrator.metrics import AdaptiveSizingPolicy, MetricsCollector, TicketOutcome
from local_first_orchestrator.symbols import SymbolIndex, contract_target_scope
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.validation import DeterministicValidator


class Phase6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "app.py").write_text(
            "def helper(value):\n    return value + 1\n\ndef calculate(value):\n    return helper(value)\n",
            encoding="utf-8",
        )
        (self.repo / "test_app.py").write_text(
            "from app import calculate\n\ndef test_calculate():\n    assert calculate(1) == 2\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.ticket = MicroTicket(
            ticket_id="T-6", objective="Change calculate only.", criterion_ids=("AC-1",),
            primary_symbol="app.py::calculate", allowed_files=("app.py", "test_app.py"),
            forbidden_changes=("Do not alter public API.",), patch_budget=PatchBudget(max_files=2, max_changed_lines=40),
            verification=VerificationProfile(commands=(("python", "-c", "print('ok')"),)),
            risk="low", review_required=True, max_attempts=2, dependencies=(),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def test_python_symbol_index_retrieves_primary_callers_and_tests(self) -> None:
        index = SymbolIndex(self.repo)
        selection = index.select_for_ticket(self.ticket)
        self.assertFalse(selection.scope_unverified)
        self.assertEqual(selection.primary.identifier, "app.py::calculate")
        self.assertIn("app.py::helper", {symbol.identifier for symbol in selection.dependencies})
        self.assertIn("test_app.py::test_calculate", {symbol.identifier for symbol in selection.tests})

        packet = ContextPacketBuilder(target_tokens=500, max_tokens=600).build_from_repository(
            self.ticket, self.repo, repository_rules="No network.", index=index,
        )
        self.assertEqual(packet.manifest["scope_verification"], "verified")
        self.assertIn("source_symbol", {section["kind"] for section in packet.manifest["sections"]})
        self.assertNotIn("unrelated repository history", packet.text)

    def test_symbol_enforcement_rejects_extra_production_symbol_and_marks_unknown_language(self) -> None:
        (self.repo / "app.py").write_text(
            "def helper(value):\n    return value + 2\n\ndef calculate(value):\n    return helper(value) + 1\n",
            encoding="utf-8",
        )
        result = DeterministicValidator(artifact_root=self.root / "artifacts").validate(
            self.repo, self.ticket, base_sha=self.base,
        )
        self.assertFalse(result.passed)
        self.assertIn("symbol scope exceeded", " ".join(result.errors))
        self.assertFalse(result.scope_unverified)

        (self.repo / "notes.txt").write_text("changed", encoding="utf-8")
        unknown_ticket = MicroTicket(**{**self.ticket.__dict__, "allowed_files": ("notes.txt",)})
        unknown = DeterministicValidator(artifact_root=self.root / "unknown").validate(
            self.repo, unknown_ticket, base_sha=self.base,
        )
        self.assertTrue(unknown.scope_unverified)
        self.assertIn("scope_unverified", unknown.compact_evidence)

    def test_file_scoped_f1_shape_builds_deterministic_packet_without_inventing_symbol(self) -> None:
        path = self.repo / "scripts" / "test-meal-planner-c0910-ui.mjs"; path.parent.mkdir()
        content = "// AUTHED expectation\nfunction makeFetchCapture() { return {}; }\n"
        path.write_text(content, encoding="utf-8")
        ticket = MicroTicket(**{**self.ticket.__dict__, "primary_symbol": "scripts/test-meal-planner-c0910-ui.mjs::fakeFetch", "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs",)})
        builder = ContextPacketBuilder(target_tokens=500, max_tokens=600)
        first = builder.build_from_repository(ticket, self.repo, repository_rules="Edit only this test file.")
        second = builder.build_from_repository(ticket, self.repo, repository_rules="Edit only this test file.")
        self.assertEqual(first.manifest, second.manifest); self.assertEqual(first.text, second.text)
        self.assertEqual(first.manifest["target_scope"], "file")
        self.assertEqual(first.manifest["primary_file"], "scripts/test-meal-planner-c0910-ui.mjs")
        self.assertIn(content, first.text)
        sections = cast(list[dict[str, object]], first.manifest["sections"])
        self.assertNotIn("source_symbol", {section["kind"] for section in sections})
        self.assertEqual(hashlib.sha256(first.text.encode()).hexdigest(), hashlib.sha256(second.text.encode()).hexdigest())

    def test_file_scope_fails_closed_for_ambiguous_missing_unsupported_and_oversized_targets(self) -> None:
        path = self.repo / "scripts" / "test-meal-planner-c0910-ui.mjs"; path.parent.mkdir(); path.write_text("function makeFetchCapture() {}\n", encoding="utf-8")
        file_ticket = MicroTicket(**{**self.ticket.__dict__, "primary_symbol": "scripts/test-meal-planner-c0910-ui.mjs::fakeFetch", "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs",)})
        with self.assertRaises(ContextBudgetError):
            ContextPacketBuilder().build_from_repository(MicroTicket(**{**file_ticket.__dict__, "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs", "test_app.py")}), self.repo, repository_rules="rules")
        with self.assertRaises(ContextBudgetError):
            ContextPacketBuilder().build_from_repository(file_ticket, self.repo / "missing", repository_rules="rules")
        unsupported = MicroTicket(**{**file_ticket.__dict__, "primary_symbol": "notes.txt::unknown", "allowed_files": ("notes.txt",)})
        (self.repo / "notes.txt").write_text("notes", encoding="utf-8")
        with self.assertRaises(ContextBudgetError): ContextPacketBuilder().build_from_repository(unsupported, self.repo, repository_rules="rules")
        path.write_text("x" * 20000, encoding="utf-8")
        with self.assertRaises(ContextBudgetError): ContextPacketBuilder(target_tokens=1, max_tokens=10).build_from_repository(file_ticket, self.repo, repository_rules="rules")

    def test_resolvable_f1_symbol_still_builds_file_scoped_packet(self) -> None:
        path = self.repo / "scripts" / "test-meal-planner-c0910-ui.mjs"; path.parent.mkdir()
        path.write_text("function fakeFetch() {}\nfunction wire() {}\nfunction main() {}\n", encoding="utf-8")
        ticket = MicroTicket(**{**self.ticket.__dict__, "primary_symbol": f"{path.relative_to(self.repo).as_posix()}::fakeFetch", "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs",)})
        self.assertEqual(contract_target_scope(ticket), "file")
        selection = SymbolIndex(self.repo).select_for_ticket(ticket)
        self.assertEqual(selection.scope, "file")
        self.assertFalse(selection.scope_unverified)
        packet = ContextPacketBuilder().build_from_repository(ticket, self.repo, repository_rules="rules")
        self.assertEqual(packet.manifest["target_scope"], "file")
        self.assertEqual(packet.manifest["primary_file"], "scripts/test-meal-planner-c0910-ui.mjs")
        self.assertNotIn("primary_symbol", packet.manifest)

    def test_file_scope_validates_multiple_functions_but_not_other_files_or_budget_overage(self) -> None:
        path = self.repo / "scripts" / "test-meal-planner-c0910-ui.mjs"; path.parent.mkdir()
        path.write_text("function fakeFetch() {}\n", encoding="utf-8")
        self.git("config", "user.email", "fixture@example.invalid"); self.git("config", "user.name", "Fixture")
        self.git("add", "."); self.git("commit", "--allow-empty", "-m", "file base")
        base = self.git("rev-parse", "HEAD").stdout.strip()
        ticket = MicroTicket(**{**self.ticket.__dict__, "primary_symbol": "scripts/test-meal-planner-c0910-ui.mjs::fakeFetch", "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs",), "patch_budget": PatchBudget(max_files=1, max_changed_lines=20)})
        path.write_text("function fakeFetch() { return 1; }\nfunction wire() {}\nfunction main() {}\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "file-artifacts").validate(self.repo, ticket, base_sha=base)
        self.assertTrue(result.passed)
        (self.repo / "other.js").write_text("const other = 1;\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "other-artifacts").validate(self.repo, ticket, base_sha=base)
        self.assertFalse(result.passed); self.assertIn("outside allowlist", " ".join(result.errors))
        (self.repo / "other.js").unlink()
        path.write_text("x\n" * 30, encoding="utf-8")
        small = MicroTicket(**{**ticket.__dict__, "patch_budget": PatchBudget(max_files=1, max_changed_lines=2)})
        result = DeterministicValidator(artifact_root=self.root / "budget-artifacts").validate(self.repo, small, base_sha=base)
        self.assertFalse(result.passed); self.assertIn("line budget exceeded", " ".join(result.errors))

    def test_symbol_scope_remains_fail_closed_and_unresolved_symbols_do_not_become_file_scope(self) -> None:
        symbol_ticket = self.ticket
        (self.repo / "app.py").write_text("def calculate(value):\n    return value + 1\n\ndef helper(value):\n    return value\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "symbol-artifacts").validate(self.repo, symbol_ticket, base_sha=self.base)
        self.assertFalse(result.passed); self.assertIn("symbol scope exceeded", " ".join(result.errors))
        unresolved = MicroTicket(**{**symbol_ticket.__dict__, "primary_symbol": "app.py::missing"})
        self.assertEqual(contract_target_scope(unresolved), "symbol")
        with self.assertRaises(ContextBudgetError): ContextPacketBuilder().build_from_repository(unresolved, self.repo, repository_rules="rules")

    def test_scope_authority_and_packet_hash_are_shared_by_ordinary_and_correction_shapes(self) -> None:
        path = self.repo / "scripts" / "test-meal-planner-c0910-ui.mjs"; path.parent.mkdir()
        path.write_text("function fakeFetch() {}\n", encoding="utf-8")
        ordinary = MicroTicket(**{**self.ticket.__dict__, "ticket_id": "ordinary", "primary_symbol": "scripts/test-meal-planner-c0910-ui.mjs::fakeFetch", "allowed_files": ("scripts/test-meal-planner-c0910-ui.mjs",)})
        correction = MicroTicket(**{**ordinary.__dict__, "ticket_id": "correction"})
        builder = ContextPacketBuilder()
        first = builder.build_from_repository(ordinary, self.repo, repository_rules="rules")
        replay = builder.build_from_repository(ordinary, self.repo, repository_rules="rules")
        second = builder.build_from_repository(correction, self.repo, repository_rules="rules")
        self.assertEqual(contract_target_scope(ordinary), contract_target_scope(correction))
        self.assertEqual(first.manifest["target_scope"], second.manifest["target_scope"])
        self.assertEqual(hashlib.sha256(first.text.encode()).hexdigest(), hashlib.sha256(replay.text.encode()).hexdigest())
        self.assertEqual(first.manifest, replay.manifest)

    def test_metrics_report_and_conservative_sizing_policy(self) -> None:
        outcomes = [TicketOutcome("T1", attempts=1, accepted=True, reverted=False, context_tokens=1000, changed_symbols=1)] * 18
        policy = AdaptiveSizingPolicy(minimum_comparable_tickets=20)
        conservative = policy.evaluate(outcomes)
        self.assertEqual(conservative.max_production_symbols, 1)
        promoted = policy.evaluate(outcomes + [TicketOutcome(f"T{i}", 1, True, False, 1000, 1) for i in range(18, 20)])
        self.assertEqual(promoted.max_production_symbols, 2)
        reduced = policy.evaluate([TicketOutcome(f"F{i}", 2, False, False, 2000, 1) for i in range(20)])
        self.assertTrue(reduced.reduce_scope)

        report = MetricsCollector().report(outcomes)
        self.assertEqual(report["first_attempt_acceptance_rate"], 1.0)
        self.assertEqual(report["average_attempts"], 1.0)
        self.assertEqual(report["context_tokens"]["p50"], 1000)


if __name__ == "__main__":
    unittest.main()
