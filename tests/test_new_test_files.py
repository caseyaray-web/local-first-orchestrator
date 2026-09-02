from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig, ticket_from_ledger
from local_first_orchestrator.generated_activation import activate_generated_ticket
from local_first_orchestrator.decomposition import (
    Criterion,
    DecompositionPlan,
    FeatureContract,
    PlanValidator,
    Tranche,
    activate_validated_plan,
)
from local_first_orchestrator.generated_projection import GeneratedProjectionDeliveryPolicy, GeneratedProjectionWorker
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.validation import DeterministicValidator


class FakeHermes:
    timeout_seconds = 5
    allow_writes = True
    is_fake = False

    def __init__(self) -> None:
        self.cards: dict[str, object] = {}

    def create_microticket(self, title: str, body: str, *, idempotency_key: str) -> str:
        task_id = f"fake-{len(self.cards) + 1}"
        self.cards[task_id] = type("Card", (), {"id": task_id, "title": title, "body": body})()
        return task_id

    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None:
        return None

    def add_comment(self, ticket_id: str, comment: str) -> None:
        return None

    def get_task(self, task_id: str) -> object:
        return self.cards[task_id]


class NewTestFilesContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("def value():\n    return 'base'\n", encoding="utf-8")
        (self.repo / "package.json").write_text('{"scripts": {}}\n', encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def feature(self) -> FeatureContract:
        return FeatureContract(
            "F-new-tests", "Declared test artifact", "Add a bounded regression test for value.",
            (Criterion("AC-1", "The new test is created only within explicitly declared test scope."),),
            ("Do not change production behavior.",), ("Only declared paths may change.",), (), self.base,
        )

    def ticket(self, *, allowed: tuple[str, ...] = ("package.json",), new: tuple[str, ...] = ("tests/test_new_behavior.py",), lines: int = 80) -> MicroTicket:
        return MicroTicket(
            "T-new-test", "Create one bounded regression test and its package registration.", ("AC-1",),
            "app.py::value", allowed,
            ("No production source changes.",), PatchBudget(2, lines),
            VerificationProfile((("python", "-m", "unittest", "tests.test_new_behavior"),), timeout_seconds=30),
            "low", True, 2, (), new,
        )

    def plan(self, ticket: MicroTicket) -> tuple[DecompositionPlan, object]:
        feature = self.feature()
        snap = snapshot(self.repo, self.base, feature)
        plan = DecompositionPlan(
            1, feature.id, feature.contract_hash, self.base, snap.snapshot_hash, (), {"all": ("AC-1",)},
            (Tranche("TR-new-test", 0, feature.objective, (), ("AC-1",), (ticket,)),),
            repository_identity=snap.repository_id, repo_snapshot_manifest_json=snap.manifest_json,
        )
        return plan, snap

    def test_missing_existing_allowed_path_remains_rejected(self) -> None:
        plan, snap = self.plan(self.ticket(allowed=("missing.py",), new=()))
        self.assertIn("unknown_file", RepositoryPlanValidator().validate(plan, snap).reasons)

    def test_declared_absent_python_test_is_accepted(self) -> None:
        ticket = self.ticket()
        plan, snap = self.plan(ticket)
        self.assertTrue(PlanValidator(max_active_tickets=1).validate(self.feature(), plan).passed)
        self.assertTrue(RepositoryPlanValidator().validate(plan, snap).passed)

    def test_declared_test_that_exists_at_base_is_rejected(self) -> None:
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_new_behavior.py").write_text("pass\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "existing test")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        plan, snap = self.plan(self.ticket())
        self.assertIn("new_test_file_already_exists", RepositoryPlanValidator().validate(plan, snap).reasons)

    def test_production_and_traversal_new_paths_are_rejected(self) -> None:
        for path, expected in (("src/new_behavior.py", "new_test_file_not_test"), ("../outside.py", "invalid_new_test_file")):
            with self.subTest(path=path):
                plan, snap = self.plan(self.ticket(new=(path,)))
                self.assertIn(expected, RepositoryPlanValidator().validate(plan, snap).reasons)

    def test_mjs_script_test_classifies_as_declared_test(self) -> None:
        plan, snap = self.plan(self.ticket(new=("scripts/test-new-behavior.mjs",)))
        self.assertTrue(RepositoryPlanValidator().validate(plan, snap).passed)

    def test_legacy_contract_omits_empty_field_and_reconstructs_empty(self) -> None:
        legacy = self.ticket(new=())
        self.assertNotIn("new_test_files", legacy.contract())
        ticket_id = self.ledger.create_ticket(title="legacy", contract=legacy.contract())
        rebuilt = ticket_from_ledger(self.ledger.get_ticket(ticket_id))
        self.assertEqual(rebuilt.new_test_files, ())

    def test_new_test_declaration_persists_and_projects_canonically(self) -> None:
        ticket = self.ticket()
        plan, snap = self.plan(ticket)
        structural = PlanValidator(max_active_tickets=1).validate(self.feature(), plan)
        repository = RepositoryPlanValidator().validate(plan, snap)
        activate_validated_plan(self.ledger, self.feature(), plan, structural, repository)
        persisted = ticket_from_ledger(self.ledger.get_ticket(ticket.ticket_id))
        self.assertEqual(persisted.new_test_files, ("tests/test_new_behavior.py",))
        fake = FakeHermes()
        result = GeneratedProjectionWorker(self.ledger, fake, GeneratedProjectionDeliveryPolicy(lease_seconds=20), worker_id="test").deliver_one()
        self.assertEqual(result.status, "delivered")
        self.assertIn('"new_test_files":["tests/test_new_behavior.py"]', fake.get_task(result.external_task_id).body)

    def test_validator_allows_only_declared_new_test_and_counts_its_lines(self) -> None:
        worktree = self.repo
        (worktree / "tests").mkdir()
        (worktree / "tests/test_new_behavior.py").write_text("\n".join("x = 1" for _ in range(20)) + "\n", encoding="utf-8")
        (worktree / "package.json").write_text('{"scripts": {"test": "python -m unittest"}}\n', encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "artifacts").validate(worktree, self.ticket(lines=200), base_sha=self.base)
        self.assertTrue(result.passed, result.errors)
        self.assertIn("tests/test_new_behavior.py", json.loads(result.full_evidence_path.read_text())["changed_files"])
        self.assertGreaterEqual(json.loads(result.full_evidence_path.read_text())["changed_lines"], 20)

    def test_validator_rejects_undeclared_or_over_budget_new_file(self) -> None:
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_undeclared.py").write_text("x = 1\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "artifacts-a").validate(self.repo, self.ticket(), base_sha=self.base)
        self.assertIn("changed path outside allowlist: tests/test_undeclared.py", result.errors)
        (self.repo / "tests/test_undeclared.py").unlink()
        (self.repo / "tests/test_new_behavior.py").write_text("\n".join("x = 1" for _ in range(81)) + "\n", encoding="utf-8")
        result = DeterministicValidator(artifact_root=self.root / "artifacts-b").validate(self.repo, self.ticket(lines=80), base_sha=self.base)
        self.assertIn("changed line budget exceeded", result.errors)
    def test_generated_e2e_creates_only_declared_test_and_reaches_done(self) -> None:
        ticket = self.ticket()
        plan, snap = self.plan(ticket)
        activate_validated_plan(
            self.ledger, self.feature(), plan,
            PlanValidator(max_active_tickets=1).validate(self.feature(), plan),
            RepositoryPlanValidator().validate(plan, snap),
        )
        board = FakeHermes()
        projected = GeneratedProjectionWorker(self.ledger, board, GeneratedProjectionDeliveryPolicy(lease_seconds=20), worker_id="projection").deliver_one()
        self.assertEqual(projected.status, "delivered")
        activation = activate_generated_ticket(ticket.ticket_id, RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,)), self.ledger)
        self.assertEqual(activation.status, "activated_ready")

        class Model:
            def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
                if purpose == "implementation":
                    assert workdir is not None
                    path = workdir / "tests/test_new_behavior.py"
                    path.parent.mkdir()
                    path.write_text("import unittest\n\nclass NewBehavior(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n", encoding="utf-8")
                    (workdir / "package.json").write_text('{"scripts":{"test-new":"python -m unittest tests.test_new_behavior"}}\n', encoding="utf-8")
                    payload = {}
                else:
                    payload = {"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "declared test created and validated"}], "findings": [], "suggestions": []}
                artifact = artifact_dir / f"{purpose}.json"
                artifact.write_text(json.dumps(payload), encoding="utf-8")
                return type("Result", (), {"payload": payload, "artifact_path": artifact})()

        self.ledger.resume("test", reason="single generated E2E")
        controller = LocalFirstController(self.ledger, board, RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,)), local_model=Model())
        self.assertTrue(controller.execute(ticket.ticket_id, repository=self.repo, allow_board_writes=True))
        self.ledger.pause("test", reason="single generated E2E complete")
        self.assertEqual(self.ledger.get_ticket(ticket.ticket_id)["state"], "done")
        self.assertIsNotNone(self.ledger.accepted_commit(ticket.ticket_id))
        attempt = self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=?", (ticket.ticket_id,)).fetchone()
        self.assertTrue((Path(attempt["worktree_path"]) / "tests/test_new_behavior.py").is_file())


if __name__ == "__main__":
    unittest.main()
