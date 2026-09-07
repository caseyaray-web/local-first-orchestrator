import json
import subprocess
import unittest
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import InjectedPaidModelAdapter
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.usage_governor import PaidPurpose, UsageGovernor


class FakeLocalPlanner:
    cost_class = "local"
    def __init__(self, proposal): self.proposal, self.calls = proposal, 0
    def propose(self, feature, snapshot, *, artifact_dir):
        self.calls += 1
        return self.proposal


def make_ticket(primary="app.py::calculate", criterion="AC-1"):
    return MicroTicket("TK-1", "Implement the bounded calculation guard.", (criterion,), primary, ("app.py", "test_app.py"), ("Do not change public APIs.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, ())


def make_feature(sha):
    return FeatureContract("F-1", "Quantity safety", "Reject negative quantity in calculate", (Criterion("AC-1", "Negative quantities are rejected"),), (), (), (), sha)


def make_plan(feature, snap, ticket=None):
    ticket = ticket or make_ticket()
    return DecompositionPlan(1, feature.id, feature.contract_hash, snap.base_sha, snap.snapshot_hash, ("Validate at calculation boundary",), {"scope": ("AC-1",)}, (Tranche("T-1", 0, "Implement the guard", ("calculation",), ("AC-1",), (ticket,)),))


class PlanningCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); root = Path(self.tmp.name)
        self.repo = root / "repo"; self.repo.mkdir()
        (self.repo / "app.py").write_text("def calculate(quantity):\n    return quantity\n")
        (self.repo / "test_app.py").write_text("def test_calculate(): pass\n")
        subprocess.run(("git", "init", "-q"), cwd=self.repo, check=True)
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.ledger = Ledger(root / "ledger.db"); self.ledger.migrate()
        self.config = RuntimeConfig(self.repo, root / "worktrees", root / "artifacts", (self.repo,))
        self.feature = make_feature(self.sha)

    def tearDown(self): self.ledger.close(); self.tmp.cleanup()

    def test_local_success_is_once_and_has_no_paid_reservation(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "activated"); self.assertEqual(planner.calls, 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from paid_reservations").fetchone()[0], 0)
        replay = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(replay.status, "already_activated"); self.assertEqual(planner.calls, 1)

    def test_plan_only_persists_pending_without_materialization_and_replays(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        first = coordinator.generate_plan_only(self.feature)
        self.assertEqual(first.status, "validated_pending_activation")
        self.assertEqual(planner.calls, 1)
        self.assertEqual(self.ledger.connection.execute("select status from decomposition_plans").fetchone()[0], "validated_pending_activation")
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from board_projection_outbox").fetchone()[0], 0)
        replay = PlanningCoordinator(self.ledger, self.config, planner).generate_plan_only(self.feature)
        self.assertEqual((replay.plan_id, replay.snapshot_hash), (first.plan_id, first.snapshot_hash))
        self.assertEqual(planner.calls, 1)

    def test_plan_only_persists_plan_that_activation_consumes(self):
        from local_first_orchestrator.repository_snapshot import snapshot, RepositoryPlanValidator
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        pending = coordinator.generate_plan_only(self.feature)
        row = self.ledger.connection.execute("select * from decomposition_plans where id=?", (pending.plan_id,)).fetchone()
        proposal = coordinator._load_plan(row)
        validation = PlanValidator().validate(self.feature, proposal)
        repository = RepositoryPlanValidator().validate(proposal, snap)
        from local_first_orchestrator.decomposition import activate_validated_plan
        activate_validated_plan(self.ledger, self.feature, proposal, validation, repository)
        self.assertEqual(self.ledger.connection.execute("select status from decomposition_plans where id=?", (pending.plan_id,)).fetchone()[0], "active")
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets").fetchone()[0], 1)
        self.assertEqual(planner.calls, 1)

    def test_plan_only_invalid_output_does_not_persist_or_materialize(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap, make_ticket(criterion="invented")))
        result = PlanningCoordinator(self.ledger, self.config, planner).generate_plan_only(self.feature)
        self.assertEqual(result.status, "structural_rejected")
        self.assertEqual(self.ledger.connection.execute("select count(*) from decomposition_plans").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets").fetchone()[0], 0)

    def test_unknown_cost_never_invokes(self):
        calls = []
        planner = LocalDecompositionPlanner(lambda *a, **k: calls.append(1))
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "unknown_cost_class"); self.assertEqual(calls, [])

    def test_paid_success_reserves_before_provider_and_replay_is_durable(self):
        governor = UsageGovernor(self.ledger); governor.configure("F-1", architecture=1, checkpoint=0, escalation=0)
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature); plan = make_plan(self.feature, snap)
        observed = []
        def provider(packet):
            observed.append(self.ledger.connection.execute("select status from paid_reservations").fetchone()[0])
            return asdict(plan)
        planner = InjectedPaidModelAdapter(self.ledger, governor, provider)
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "activated"); self.assertEqual(observed, ["in_flight"])
        self.assertEqual(governor.usage("F-1", PaidPurpose.ARCHITECTURE)["completed"], 1)
        replay = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(replay.status, "already_activated"); self.assertEqual(len(observed), 1)

    def test_budget_exhausted_does_not_call_provider(self):
        governor = UsageGovernor(self.ledger); governor.configure("F-1", architecture=0, checkpoint=0, escalation=0)
        calls = []
        planner = InjectedPaidModelAdapter(self.ledger, governor, lambda p: calls.append(p) or {})
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "budget_exhausted"); self.assertEqual(calls, [])

    def test_structural_rejection_is_recorded_and_not_retried(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        bad = make_plan(self.feature, snap, make_ticket(criterion="invented"))
        planner = FakeLocalPlanner(bad); coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        first = coordinator.plan(self.feature); second = coordinator.plan(self.feature)
        self.assertEqual(first.status, "structural_rejected"); self.assertEqual(second.status, "structural_rejected"); self.assertEqual(planner.calls, 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from decomposition_plans").fetchone()[0], 0)

    def test_completed_response_recovers_without_second_planner_call(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature); planner = FakeLocalPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        with patch("local_first_orchestrator.planning_coordinator.activate_validated_plan", side_effect=RuntimeError("crash before activation")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                coordinator.plan(self.feature)
        recovered = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(recovered.status, "activated"); self.assertEqual(planner.calls, 1)

    def test_repository_rejection_is_recorded(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap, make_ticket("missing.py::calculate")))
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "repository_rejected")
        self.assertEqual(self.ledger.connection.execute("select count(*) from decomposition_plans").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
