import json
import subprocess
import unittest
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.admission import FeatureAdmissionSpec, FileDisposition
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, planner_contract, planner_contract_hash
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import InjectedPaidModelAdapter
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.usage_governor import PaidPurpose, UsageGovernor


class FakeLocalPlanner:
    cost_class = "local"
    role = "decomposition"
    provider = "fixture-provider"
    model = "fixture-local-planner"
    profile = "fixture-profile"
    routing_source = "fixture"
    planner_contract_hash = planner_contract_hash()
    def __init__(self, proposal): self.proposal, self.calls, self.repository = proposal, 0, None
    def propose(self, feature, snapshot, *, artifact_dir, repository=None):
        self.calls += 1; self.repository = repository
        return self.proposal


class FakeStandardPlanner(FakeLocalPlanner):
    cost_class = "standard"
    model = "fixture-standard-planner"


class RejectOnceValidator:
    def validate(self, feature, proposal):
        from local_first_orchestrator.decomposition import PlanValidationResult
        return PlanValidationResult(False, ("criterion_uncovered",))


def make_ticket(primary="app.py::calculate", criterion="AC-1"):
    return MicroTicket("TK-1", "Implement the bounded calculation guard.", (criterion,), primary, ("app.py", "test_app.py"), ("Do not change public APIs.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, ())


def make_feature(sha):
    return FeatureContract("F-1", "Quantity safety", "Reject negative quantity in calculate", (Criterion("AC-1", "Negative quantities are rejected"),), (), (), (), sha)


def make_plan(feature, snap, ticket=None):
    ticket = ticket or make_ticket()
    return DecompositionPlan(1, feature.id, feature.contract_hash, snap.base_sha, snap.snapshot_hash, ("Validate at calculation boundary",), {"AC-1": (ticket.ticket_id,)}, (Tranche("T-1", 0, "Implement the guard", ("calculation",), ("AC-1",), (ticket,)),))


def make_multi_plan(feature, snap):
    first = MicroTicket("TK-2", "Implement the bounded calculation guard.", ("AC-1",), "app.py::calculate", ("app.py", "test_app.py"), ("Do not change public APIs.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, ())
    second = MicroTicket("TK-1", "Verify the bounded calculation guard.", ("AC-1",), "test_app.py::test_calculate", ("app.py", "test_app.py"), ("Do not change public APIs.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 2, ("TK-2",))
    return DecompositionPlan(1, feature.id, feature.contract_hash, snap.base_sha, snap.snapshot_hash, ("Validate at calculation boundary",), {"AC-1": (first.ticket_id, second.ticket_id)}, (Tranche("T-1", 0, "Implement the guard", ("calculation",), ("AC-1",), (first, second)),))


class PlanningCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); root = Path(self.tmp.name); self.root = root
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
        admitted = FeatureAdmissionSpec(self.feature.id, self.feature.title, "T-1", "Implement the guard", self.feature.objective, self.sha, self.feature.acceptance_criteria, (), (), (), (FileDisposition("app.py", "modify"), FileDisposition("test_app.py", "modify")), None)
        self.ledger.connection.execute("insert into feature_contracts(feature_id,contract_hash,contract_json,created_at) values (?,?,?,?)", (self.feature.id, self.feature.contract_hash, json.dumps({"spec": admitted.canonical_payload}, sort_keys=True, separators=(",", ":")), 1))

    def tearDown(self): self.ledger.close(); self.tmp.cleanup()

    def test_route_bound_identity_allows_luna_rejection_then_terra_fresh_plan(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        luna = FakeLocalPlanner(make_plan(self.feature, snap))
        first_coordinator = PlanningCoordinator(self.ledger, self.config, luna, plan_validator=RejectOnceValidator())
        first = first_coordinator.generate_plan_only(self.feature)
        self.assertEqual(first.status, "structural_rejected")
        self.assertEqual(luna.calls, 1)
        first_key = first.request_key
        first_artifact = first_coordinator._artifact_dir(self.feature, str(first_key))
        first_row = dict(self.ledger.connection.execute("select * from planning_runs where request_key=?", (first_key,)).fetchone())

        terra = FakeStandardPlanner(make_plan(self.feature, snap))
        second_coordinator = PlanningCoordinator(self.ledger, self.config, terra)
        second = second_coordinator.generate_plan_only(self.feature)
        self.assertEqual(second.status, "validated_pending_activation")
        self.assertEqual(terra.calls, 1)
        self.assertNotEqual(first_key, second.request_key)
        self.assertTrue(first_artifact.exists())
        self.assertTrue(second_coordinator._artifact_dir(self.feature, str(second.request_key)).exists())
        self.assertEqual(dict(self.ledger.connection.execute("select * from planning_runs where request_key=?", (first_key,)).fetchone()), first_row)
        terra_run = self.ledger.connection.execute("select planner_contract_hash from planning_runs where request_key=?", (second.request_key,)).fetchone()
        from local_first_orchestrator.decomposition_planner import planner_contract_hash
        self.assertEqual(terra_run["planner_contract_hash"], getattr(terra, "planner_contract_hash", planner_contract_hash()))
        replay = second_coordinator.generate_plan_only(self.feature)
        self.assertEqual(replay.plan_id, second.plan_id)
        self.assertEqual(terra.calls, 1)

    def test_route_components_change_request_identity_and_same_route_repeats(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        def key(**changes):
            planner = FakeStandardPlanner(make_plan(self.feature, snap))
            for name, value in changes.items(): setattr(planner, name, value)
            return PlanningCoordinator(self.ledger, self.config, planner)._request_key(self.feature, snap)
        baseline = key()
        self.assertNotEqual(baseline, key(provider="other-provider"))
        self.assertNotEqual(baseline, key(model="other-model"))
        self.assertNotEqual(baseline, key(profile="other-profile"))
        self.assertNotEqual(baseline, key(cost_class="local"))
        self.assertEqual(baseline, key())

    def test_pending_plan_contract_change_invokes_fresh_planner_then_replays_v2(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        v1 = FakeStandardPlanner(make_plan(self.feature, snap))
        first = PlanningCoordinator(self.ledger, self.config, v1).generate_plan_only(self.feature)
        changed = planner_contract(); changed["output_rules"] = list(changed["output_rules"]) + ["Use a separate bounded seam."]
        v2 = FakeStandardPlanner(make_plan(self.feature, snap)); v2.planner_contract_hash = planner_contract_hash(contract=changed)
        second_coordinator = PlanningCoordinator(self.ledger, self.config, v2)
        second = second_coordinator.generate_plan_only(self.feature)
        self.assertEqual(first.status, "validated_pending_activation")
        self.assertEqual(second.status, "validated_pending_activation")
        self.assertNotEqual(first.request_key, second.request_key)
        self.assertEqual(v1.calls, 1); self.assertEqual(v2.calls, 1)
        replay = second_coordinator.generate_plan_only(self.feature)
        self.assertEqual(replay.request_key, second.request_key); self.assertEqual(v2.calls, 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from planning_runs where request_key=?", (first.request_key,)).fetchone()[0], 1)

    def test_missing_or_malformed_persisted_contract_fails_closed_before_snapshot(self):
        missing = Ledger(self.root / "missing.db"); missing.migrate()
        with self.assertRaisesRegex(ValueError, "persisted feature contract authority is missing"):
            PlanningCoordinator(missing, self.config, FakeStandardPlanner(make_plan(self.feature, snapshot(self.repo, self.sha, self.feature)))).generate_plan_only(self.feature)
        missing.close()
        admitted = FeatureAdmissionSpec(self.feature.id, self.feature.title, "T-1", "Implement the guard", self.feature.objective, self.sha, self.feature.acceptance_criteria, (), (), (), (FileDisposition("app.py", "modify"),), None)
        malformed = Ledger(self.root / "malformed.db"); malformed.migrate(); malformed.connection.execute("insert into feature_contracts(feature_id,contract_hash,contract_json,created_at) values (?,?,?,?)", (self.feature.id, self.feature.contract_hash, "{}", 1))
        with self.assertRaisesRegex(ValueError, "persisted feature contract envelope is malformed"):
            PlanningCoordinator(malformed, self.config, FakeStandardPlanner(make_plan(self.feature, snapshot(self.repo, self.sha, self.feature)))).generate_plan_only(self.feature)
        malformed.close()

    def test_conflicting_route_provenance_for_same_request_fails_closed(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeStandardPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        snap = coordinator._snapshot_for_feature(self.feature, self.sha)
        key = coordinator._request_key(self.feature, snap)
        artifact = coordinator._artifact_dir(self.feature, key); artifact.mkdir(parents=True)
        (artifact / "planner-response.json").write_text(coordinator._plan_json(planner.proposal))
        coordinator._record(key, self.feature, snap, status="structural_rejected", artifact=artifact / "planner-response.json", structural=("criterion_uncovered",))
        self.ledger.connection.execute("update planning_runs set planner_model='conflicting-model' where request_key=?", (key,))
        result = coordinator.generate_plan_only(self.feature)
        self.assertEqual(result.status, "planner_failed")
        self.assertIn("conflicting durable planner route provenance", result.reasons)
        self.assertEqual(planner.calls, 0)

    def test_local_success_is_once_and_has_no_paid_reservation(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        result = PlanningCoordinator(self.ledger, self.config, planner).plan(self.feature)
        self.assertEqual(result.status, "activated"); self.assertEqual(planner.calls, 1)
        run = self.ledger.connection.execute("select status,plan_id,ticket_ids_json from planning_runs").fetchone()
        self.assertEqual((run["status"], run["plan_id"], json.loads(run["ticket_ids_json"])), ("activated", result.plan_id, list(result.activated_ticket_ids)))
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
        coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        self.assertEqual(self.ledger.connection.execute("select status from decomposition_plans where id=?", (pending.plan_id,)).fetchone()[0], "active")
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets").fetchone()[0], 1)
        run = self.ledger.connection.execute("select status,plan_id,ticket_ids_json from planning_runs").fetchone()
        self.assertEqual((run["status"], run["plan_id"], json.loads(run["ticket_ids_json"])), ("activated", pending.plan_id, ["TK-1"]))
        self.assertEqual(planner.calls, 1)

    def test_activation_failure_does_not_finalize_planning_run(self):
        from unittest.mock import patch
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        pending = coordinator.generate_plan_only(self.feature)
        with patch("local_first_orchestrator.planning_coordinator.activate_validated_plan", side_effect=RuntimeError("activation failed")):
            with self.assertRaisesRegex(RuntimeError, "activation failed"):
                coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        self.assertEqual(self.ledger.connection.execute("select status from planning_runs").fetchone()[0], "validated_pending_activation")

    def test_injected_finalization_failure_rolls_back_activation_and_materialization(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        pending = coordinator.generate_plan_only(self.feature)
        with patch.object(self.ledger, "_finalize_planning_run_activation", side_effect=RuntimeError("finalization failed")):
            with self.assertRaisesRegex(RuntimeError, "finalization failed"):
                coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        self.assertEqual(self.ledger.connection.execute("select status from decomposition_plans").fetchone()[0], "validated_pending_activation")
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("select status from planning_runs").fetchone()[0], "validated_pending_activation")

    def test_finalizer_rejects_wrong_plan_and_ticket_identity_from_terra_exploit(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        planner = FakeLocalPlanner(make_plan(self.feature, snapshot(self.repo, self.sha, self.feature)))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        pending = coordinator.generate_plan_only(self.feature)
        coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        with self.assertRaisesRegex(ValueError, "ticket identity"):
            self.ledger.finalize_planning_run_activation(str(pending.request_key), plan_id=str(pending.plan_id), ticket_ids=("wrong-ticket",))
        with self.assertRaisesRegex(ValueError, "plan identity"):
            self.ledger.finalize_planning_run_activation(str(pending.request_key), plan_id="plan-other", ticket_ids=("TK-1",))
        run = self.ledger.connection.execute("select status,plan_id,ticket_ids_json from planning_runs").fetchone()
        self.assertEqual((run["status"], run["plan_id"], json.loads(run["ticket_ids_json"])), ("activated", pending.plan_id, ["TK-1"]))

    def test_nonlexical_multi_ticket_order_uses_canonical_ledger_order_everywhere(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature)
        planner = FakeLocalPlanner(make_multi_plan(self.feature, snap))
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        pending = coordinator.generate_plan_only(self.feature)
        first = coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        self.assertEqual(first.activated_ticket_ids, ("TK-1", "TK-2"))
        run = self.ledger.connection.execute("select ticket_ids_json from planning_runs").fetchone()
        self.assertEqual(json.loads(run["ticket_ids_json"]), ["TK-1", "TK-2"])
        replay = coordinator.activate_persisted_plan(self.feature, request_key=str(pending.request_key), plan_id=str(pending.plan_id))
        self.assertEqual(replay.activated_ticket_ids, first.activated_ticket_ids)
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


    def test_coordinator_passes_canonical_repository_to_planner(self):
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature); planner = FakeLocalPlanner(make_plan(self.feature, snap)); coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        coordinator.generate_plan_only(self.feature)
        self.assertIsNotNone(planner.repository); self.assertEqual(Path(planner.repository or "").resolve(), self.repo.resolve())

    def test_coordinator_mutation_tripwire_fails_closed(self):
        from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, planner_contract, planner_contract_hash
        from local_first_orchestrator.repository_snapshot import snapshot
        snap = snapshot(self.repo, self.sha, self.feature); raw = PlanningCoordinator(self.ledger, self.config, FakeLocalPlanner(make_plan(self.feature, snap)))._plan_json(make_plan(self.feature, snap)); target = self.repo / "app.py"
        def run(argv, **kwargs):
            target.write_text("mutated by planner\\n")
            return subprocess.CompletedProcess(argv, 0, raw, "")
        planner = LocalDecompositionPlanner(run, cost_class="local", provider="p", model="m")
        result = PlanningCoordinator(self.ledger, self.config, planner).generate_plan_only(self.feature)
        self.assertEqual(result.status, "planner_failed"); self.assertEqual(self.ledger.connection.execute("select count(*) from decomposition_plans").fetchone()[0], 0); self.assertIn("mutated", target.read_text())
        artifact = PlanningCoordinator(self.ledger, self.config, planner)._artifact_dir(self.feature, str(result.request_key))
        self.assertTrue((artifact / "protected-before.json").exists()); self.assertTrue((artifact / "protected-after.json").exists())

    def test_coordinator_timeout_retains_post_fingerprint(self):
        from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, planner_contract, planner_contract_hash
        def run(argv, **kwargs): raise subprocess.TimeoutExpired(argv, 1)
        planner = LocalDecompositionPlanner(run, cost_class="local", provider="p", model="m")
        result = PlanningCoordinator(self.ledger, self.config, planner).generate_plan_only(self.feature)
        self.assertEqual(result.status, "planner_timeout")
        artifact = PlanningCoordinator(self.ledger, self.config, planner)._artifact_dir(self.feature, str(result.request_key))
        self.assertTrue((artifact / "protected-before.json").exists()); self.assertTrue((artifact / "protected-after.json").exists())

if __name__ == "__main__": unittest.main()
