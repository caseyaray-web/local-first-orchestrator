from __future__ import annotations

import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.decomposition import (
    Criterion,
    DecompositionPlan,
    FeatureContract,
    PlanValidator,
    Tranche,
    activate_validated_plan,
)
from local_first_orchestrator.generated_activation import activate_generated_ticket
from local_first_orchestrator.generated_projection import (
    GeneratedProjectionDeliveryPolicy,
    GeneratedProjectionWorker,
)
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from tests.test_generated_projection_delivery import _FAKE_HERMES
from tests.test_runtime_milestone2 import FakeBoard, FakeModel


class SequencedRepairModel:
    """Fake model boundary that makes the review-driven repair explicit."""

    def __init__(self, actions, review_payloads) -> None:
        self.actions = list(actions)
        self.review_payloads = list(review_payloads)
        self.calls = []

    def invoke(self, purpose, packet, *, artifact_dir, workdir=None):
        self.calls.append((purpose, packet, Path(workdir) if workdir is not None else None))
        if purpose == "implementation":
            self.actions.pop(0)(Path(workdir))
            payload = {}
        else:
            payload = self.review_payloads.pop(0)
        return type("Result", (), {"payload": payload})()


class GeneratedMicroticketEndToEndTests(unittest.TestCase):
    """One fake-boundary proof from snapshot through accepted local execution."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        (self.repo / "app.py").write_text("def value():\n    return 'bad'\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD").stdout.strip()

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.fake_hermes = self.root / "hermes"
        self.fake_hermes.write_text(_FAKE_HERMES, encoding="utf-8")
        self.fake_hermes.chmod(stat.S_IRWXU)
        self.projection_log = self.root / "projection.log"
        self.projection_state = self.root / "projection.json"
        self.old_environment = {
            key: os.environ.get(key)
            for key in ("GENERATED_PROJECTION_LOG", "GENERATED_PROJECTION_STATE", "GENERATED_PROJECTION_MODE")
        }
        os.environ["GENERATED_PROJECTION_LOG"] = str(self.projection_log)
        os.environ["GENERATED_PROJECTION_STATE"] = str(self.projection_state)
        os.environ.pop("GENERATED_PROJECTION_MODE", None)
        self.config = RuntimeConfig(
            self.repo,
            self.root / "worktrees",
            self.root / "artifacts",
            repository_allowlist=(self.repo,),
        )

    def tearDown(self) -> None:
        self.ledger.close()
        for key, value in self.old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    @staticmethod
    def implement_value(path: Path) -> None:
        (path / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")

    def test_generated_microticket_snapshot_to_accepted_execution(self) -> None:
        feature = FeatureContract(
            "feature-value",
            "Return fixture value",
            "Make value return ok.",
            (Criterion("AC-1", "value returns ok", "run the fixture assertion"),),
            ("No API expansion.",),
            ("Only app.py changes.",),
            (),
            self.base_sha,
        )
        repository_snapshot = snapshot(self.repo, self.base_sha, feature)
        ticket = MicroTicket(
            "generated-value",
            "Make the fixture value return ok.",
            ("AC-1",),
            "app.py::value",
            ("app.py",),
            ("No API change.",),
            PatchBudget(1, 10),
            VerificationProfile((("python", "-c", "from app import value; assert value() == 'ok'"),)),
            "low",
            True,
            1,
            (),
        )
        plan = DecompositionPlan(
            1,
            feature.id,
            feature.contract_hash,
            repository_snapshot.base_sha,
            repository_snapshot.snapshot_hash,
            ("Edit only the fixture function.",),
            {"active": ("AC-1",)},
            (Tranche("active", 0, "Implement fixture value", ("value",), ("AC-1",), (ticket,)),),
            repository_identity=repository_snapshot.repository_id,
            repo_snapshot_manifest_json=repository_snapshot.manifest_json,
        )

        plan_validation = PlanValidator().validate(feature, plan)
        repository_validation = RepositoryPlanValidator().validate(plan, repository_snapshot)
        self.assertTrue(plan_validation.passed, plan_validation.reasons)
        self.assertTrue(repository_validation.passed, repository_validation.reasons)
        _, materialized = activate_validated_plan(
            self.ledger, feature, plan, plan_validation, repository_validation
        )
        self.assertEqual(materialized, (ticket.ticket_id,))
        self.assertEqual(self.ledger.get_ticket(ticket.ticket_id)["state"], "draft")

        adapter = HermesBoardAdapter(
            executable=str(self.fake_hermes), board="board", allow_writes=True, timeout_seconds=2
        )
        delivered = GeneratedProjectionWorker(
            self.ledger,
            adapter,
            GeneratedProjectionDeliveryPolicy(lease_seconds=15, retry_delay=5),
            worker_id="projection-worker",
            clock=lambda: 100,
        ).deliver_one()
        self.assertEqual((delivered.status, delivered.external_task_id), ("delivered", "1"))
        calls = [json.loads(line) for line in self.projection_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([call[3] for call in calls], ["create", "show"])

        activation = activate_generated_ticket(ticket.ticket_id, self.config, self.ledger)
        self.assertEqual(activation.status, "activated_ready")
        self.assertEqual(self.ledger.get_ticket(ticket.ticket_id)["state"], "ready_local")

        controller_board = FakeBoard()
        model = FakeModel([self.implement_value])
        controller = LocalFirstController(self.ledger, controller_board, self.config, local_model=model)
        self.assertTrue(controller.execute(ticket.ticket_id, repository=self.repo, allow_board_writes=True))

        row = self.ledger.get_ticket(ticket.ticket_id)
        self.assertEqual(row["state"], "done")
        self.assertTrue(self.ledger.accepted_commit(ticket.ticket_id))
        self.assertEqual([purpose for purpose, _, _ in model.calls], ["implementation", "review"])
        self.assertNotEqual(model.calls[0][1], model.calls[1][1])
        self.assertEqual([state for _, state, _ in controller_board.writes], ["done"])
        validation_stage = self.ledger.runtime_stage(ticket.ticket_id, "validation-1")
        self.assertIsNotNone(validation_stage)
        assert validation_stage is not None
        self.assertEqual(validation_stage["detail"], "validation passed")
        self.assertIsNotNone(self.ledger.runtime_stage(ticket.ticket_id, "review_completed"))

    def test_generated_ticket_repairs_in_same_worktree_after_fresh_review(self) -> None:
        (self.repo / "app.py").write_text(
            "def greeting(name):\n    return 'Hello, stranger'\n", encoding="utf-8"
        )
        self.git("add", "app.py")
        self.git("commit", "-m", "greeting fixture")
        base_sha = self.git("rev-parse", "HEAD").stdout.strip()

        feature = FeatureContract(
            "feature-greeting",
            "Return a greeting for a supplied name",
            "Make greeting include the supplied name.",
            (Criterion("AC-1", "greeting includes the supplied name", "run the fixture assertion"),),
            ("No API expansion.",),
            ("Only app.py changes.",),
            (),
            base_sha,
        )
        repository_snapshot = snapshot(self.repo, base_sha, feature)
        ticket = MicroTicket(
            "generated-greeting",
            "Make greeting include the supplied name.",
            ("AC-1",),
            "app.py::greeting",
            ("app.py",),
            ("No API change.",),
            PatchBudget(1, 10),
            VerificationProfile(
                (("python", "-B", "-c", "from app import greeting; assert greeting('Ada').startswith('Hello')"),)
            ),
            "low",
            True,
            2,
            (),
        )
        plan = DecompositionPlan(
            1,
            feature.id,
            feature.contract_hash,
            repository_snapshot.base_sha,
            repository_snapshot.snapshot_hash,
            ("Edit only the fixture function.",),
            {"active": ("AC-1",)},
            (Tranche("active", 0, "Implement fixture greeting", ("greeting",), ("AC-1",), (ticket,)),),
            repository_identity=repository_snapshot.repository_id,
            repo_snapshot_manifest_json=repository_snapshot.manifest_json,
        )
        plan_validation = PlanValidator().validate(feature, plan)
        repository_validation = RepositoryPlanValidator().validate(plan, repository_snapshot)
        self.assertTrue(plan_validation.passed, plan_validation.reasons)
        self.assertTrue(repository_validation.passed, repository_validation.reasons)
        _, materialized = activate_validated_plan(
            self.ledger, feature, plan, plan_validation, repository_validation
        )
        self.assertEqual(materialized, (ticket.ticket_id,))

        adapter = HermesBoardAdapter(
            executable=str(self.fake_hermes), board="board", allow_writes=True, timeout_seconds=2
        )
        delivered = GeneratedProjectionWorker(
            self.ledger,
            adapter,
            GeneratedProjectionDeliveryPolicy(lease_seconds=15, retry_delay=5),
            worker_id="projection-worker",
            clock=lambda: 100,
        ).deliver_one()
        self.assertEqual((delivered.status, delivered.external_task_id), ("delivered", "1"))
        activation = activate_generated_ticket(ticket.ticket_id, self.config, self.ledger)
        self.assertEqual(activation.status, "activated_ready")

        def incomplete_greeting(path: Path) -> None:
            (path / "app.py").write_text("def greeting(name):\n    return 'Hello'\n", encoding="utf-8")

        def repaired_greeting(path: Path) -> None:
            (path / "app.py").write_text(
                "def greeting(name):\n    return f'Hello, {name}'\n", encoding="utf-8"
            )

        repair_finding = {
            "criterion_id": "AC-1",
            "severity": "blocking",
            "file": "app.py",
            "symbol": "greeting",
            "evidence": "greeting('Ada') returns Hello without the supplied name",
            "minimal_repair": "Include name in the greeting result.",
            "verification": "python -c \"from app import greeting; assert 'Ada' in greeting('Ada')\"",
            "fingerprint_input": "omits supplied name",
        }
        model = SequencedRepairModel(
            [incomplete_greeting, repaired_greeting],
            [
                {
                    "verdict": "repair",
                    "criterion_results": [
                        {"criterion_id": "AC-1", "status": "fail", "evidence": repair_finding["evidence"]}
                    ],
                    "findings": [repair_finding],
                    "suggestions": [],
                },
                {
                    "verdict": "pass",
                    "criterion_results": [
                        {"criterion_id": "AC-1", "status": "pass", "evidence": "Ada is included"}
                    ],
                    "findings": [],
                    "suggestions": [],
                },
            ],
        )
        controller_board = FakeBoard()
        controller = LocalFirstController(self.ledger, controller_board, self.config, local_model=model)
        self.assertTrue(controller.execute(ticket.ticket_id, repository=self.repo, allow_board_writes=True))

        row = self.ledger.get_ticket(ticket.ticket_id)
        self.assertEqual(row["state"], "done")
        self.assertTrue(self.ledger.accepted_commit(ticket.ticket_id))
        self.assertEqual([purpose for purpose, _, _ in model.calls], ["implementation", "review", "implementation", "review"])
        implementation_worktrees = [workdir for purpose, _, workdir in model.calls if purpose == "implementation"]
        self.assertEqual(implementation_worktrees[0], implementation_worktrees[1])
        self.assertNotEqual(implementation_worktrees[0], self.repo)
        review_packets = [packet for purpose, packet, _ in model.calls if purpose == "review"]
        self.assertNotEqual(review_packets[0], review_packets[1])
        self.assertIn("return 'Hello'", review_packets[0])
        self.assertIn("Hello, {name}", review_packets[1])
        self.assertEqual([state for _, state, _ in controller_board.writes], ["done"])
        self.assertEqual(self.ledger.runtime_stage(ticket.ticket_id, "validation-1")["detail"], "validation passed")
        self.assertEqual(self.ledger.runtime_stage(ticket.ticket_id, "validation-2")["detail"], "validation passed")
        findings = self.ledger.connection.execute(
            "SELECT attempt_number FROM review_findings WHERE ticket_id = ?", (ticket.ticket_id,)
        ).fetchall()
        self.assertEqual([finding["attempt_number"] for finding in findings], [1])
        self.assertEqual(
            self.ledger.connection.execute(
                "SELECT COUNT(*) FROM tickets WHERE parent_ticket_id = ?", (ticket.ticket_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.ledger.connection.execute(
                "SELECT COUNT(*) FROM decomposition_plans WHERE feature_id = ?", (feature.id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.ledger.connection.execute(
                "SELECT COUNT(*) FROM events WHERE entity_id = ? AND to_state = 'needs_triage'",
                (ticket.ticket_id,),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
