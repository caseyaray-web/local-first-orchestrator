import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from local_first_orchestrator.decomposition import Criterion, FeatureContract
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner, packet, planner_contract, planner_contract_hash, planner_schema
from local_first_orchestrator.repository_snapshot import RepositorySnapshot
from local_first_orchestrator.ticket import PatchBudgetPolicy
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.controller import RuntimeConfig


class PlannerContractIdentityTests(unittest.TestCase):
    def setUp(self):
        self.feature = FeatureContract("F", "Feature", "Implement the bounded concern", (Criterion("AC", "Do the concern"),), (), (), (), "base")
        self.snapshot = RepositorySnapshot("repo", "base", (), ())
        self.config = RuntimeConfig(Path(tempfile.mkdtemp()) / "repo", Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()))

    def test_identical_contract_hash_is_deterministic(self):
        self.assertEqual(planner_contract_hash(), planner_contract_hash())
        self.assertEqual(planner_contract_hash(contract=planner_contract()), planner_contract_hash(contract=planner_contract()))

    def test_schema_change_changes_contract_hash(self):
        changed = copy.deepcopy(planner_contract())
        changed["schema"]["root"]["required"].append("new_semantic_field")
        self.assertNotEqual(planner_contract_hash(), planner_contract_hash(contract=changed))

    def test_budget_policy_change_changes_contract_hash(self):
        changed = planner_contract(budget_policy=PatchBudgetPolicy(1, 100, 20))
        self.assertNotEqual(planner_contract_hash(), planner_contract_hash(contract=changed))

    def test_output_instruction_change_changes_contract_hash(self):
        changed = copy.deepcopy(planner_contract())
        changed["output_rules"].append("Use a distinct bounded architectural seam.")
        self.assertNotEqual(planner_contract_hash(), planner_contract_hash(contract=changed))

    def test_ephemeral_values_are_not_in_production_contract(self):
        contract = planner_contract()
        self.assertNotIn("cwd", contract)
        self.assertNotIn("timestamp", contract)
        self.assertNotIn("artifact_dir", contract)
        self.assertEqual(planner_contract_hash(), planner_contract_hash(contract=contract))

    def test_packet_exposes_contract_hash_and_contract_semantics(self):
        body = json.loads(packet(self.feature, self.snapshot))
        self.assertEqual(body["planner_contract_hash"], planner_contract_hash())
        self.assertEqual(body["patch_budget_policy"]["normal_max_files"], 2)
        self.assertEqual(body["output_contract"]["schema"], planner_schema())

    def test_contract_hash_binds_request_key(self):
        first = LocalDecompositionPlanner(cost_class="standard", provider="provider", model="model", profile="profile", role="decomposition", routing_source="fixture")
        second = LocalDecompositionPlanner(cost_class="standard", provider="provider", model="model", profile="profile", role="decomposition", routing_source="fixture")
        second.planner_contract_hash = planner_contract_hash(budget_policy=PatchBudgetPolicy(1, 100, 20))
        coordinator_a = PlanningCoordinator(None, self.config, first)  # type: ignore[arg-type]
        coordinator_b = PlanningCoordinator(None, self.config, second)  # type: ignore[arg-type]
        self.assertNotEqual(coordinator_a._request_key(self.feature, self.snapshot), coordinator_b._request_key(self.feature, self.snapshot))


if __name__ == "__main__":
    unittest.main()
