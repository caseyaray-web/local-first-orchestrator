import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.admission import FeatureAdmissionSpec, FileDisposition
from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, Tranche
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.repository_snapshot import snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class Route:
    cost_class = "standard"
    role = "decomposition"
    provider = "fixture"
    model = "fake"
    profile = "fixture"
    routing_source = "fixture"
    planner_contract_hash = "planner-fixture"


class SnapshotRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-b", "main"), cwd=self.repo, check=True, capture_output=True)
        subprocess.run(("git", "config", "user.email", "fixture@example.invalid"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Fixture"), cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("def run_app():\n    return 1\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-m", "base"), cwd=self.repo, check=True, capture_output=True)
        self.base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.db = self.root / "ledger.db"
        self.ledger = Ledger(self.db)
        self.ledger.migrate()
        self.spec = FeatureAdmissionSpec("F", "Feature", "Objective", "T0", "Tranche", self.base, (Criterion("A", "A"),), (), (), (), (FileDisposition("app.py", "modify"),))
        self.feature = self.spec.contract
        self.s1 = snapshot(self.repo, self.base, self.feature, limit=1)
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        self.coordinator = PlanningCoordinator(self.ledger, self.config, Route())
        now = 1
        envelope = json.dumps({"spec": self.spec.canonical_payload}, sort_keys=True, separators=(",", ":"))
        self.ledger.connection.execute("INSERT INTO feature_contracts(feature_id,contract_hash,contract_json,created_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES (?,?,?,?,?,?,?,?)", ("F", self.feature.contract_hash, envelope, now, str(self.repo.resolve()), self.base, self.s1.snapshot_hash, self.s1.manifest_json))

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def plan_for(self, snap):
        ticket = MicroTicket("TK-1", "Implement the bounded app behavior", ("A",), "app.py::run_app", ("app.py",), ("Do not change unrelated behavior.",), PatchBudget(), VerificationProfile((("python", "-m", "unittest"),)), "low", True, 1, ())
        plan = DecompositionPlan(1, "F", self.feature.contract_hash, self.base, snap.snapshot_hash, (), {"A": ("TK-1",)}, (Tranche("T0", 0, "Implement", (), ("A",), (ticket,)),), repository_identity=snap.repository_id, repo_snapshot_manifest_json=snap.manifest_json)
        return plan

    def test_revalidation_authorizes_pending_plan_and_activation(self):
        s2 = self.coordinator._snapshot_for_feature(self.feature, self.base)
        plan = self.plan_for(s2)
        raw = json.dumps({"feature": self.feature.__dict__, "plan": plan.__dict__}, default=lambda x: x.__dict__ if hasattr(x, "__dict__") else list(x), sort_keys=True, separators=(",", ":"))
        fp = hashlib.sha256(raw.encode()).hexdigest()
        self.ledger.persist_validated_decomposition_plan(plan_id="plan-test", feature_id="F", fingerprint=fp, plan_json=raw, repository_identity=s2.repository_id, repo_base_sha=self.base, repo_snapshot_hash=s2.snapshot_hash, repo_snapshot_manifest_json=s2.manifest_json)
        self.ledger.connection.execute("INSERT INTO planning_runs(request_key,feature_id,contract_hash,repo_base_sha,repo_snapshot_hash,planner_identity,cost_class,status,plan_id,created_at,updated_at,repository_identity,repo_snapshot_manifest_json,planner_role,planner_provider,planner_model,planner_profile,planner_routing_source,planner_contract_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("req", "F", self.feature.contract_hash, self.base, s2.snapshot_hash, "fixture", "standard", "validated_pending_activation", "plan-test", 1, 1, str(self.repo.resolve()), s2.manifest_json, "decomposition", "fixture", "fake", "fixture", "fixture", "planner-fixture"))
        with self.assertRaisesRegex(ValueError, "repository provenance conflicts"):
            self.coordinator.activate_persisted_plan(self.feature, request_key="req", plan_id="plan-test")
        revalidated = self.coordinator.revalidate_feature_repository_snapshot("F")
        self.assertEqual((revalidated["generation"], revalidated["source_snapshot_hash"], revalidated["target_snapshot_hash"]), (1, self.s1.snapshot_hash, s2.snapshot_hash))
        expected = self.ledger.snapshot_revalidation_hash(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=self.s1.snapshot_hash, target_snapshot_hash=s2.snapshot_hash, generation=1)
        self.assertEqual(revalidated["revalidation_hash"], expected)
        activated = self.coordinator.activate_persisted_plan(self.feature, request_key="req", plan_id="plan-test")
        self.assertEqual(activated.activated_ticket_ids, ("TK-1",))
        self.assertEqual(self.ledger.feature_snapshot_authority("F")["snapshot_hash"], s2.snapshot_hash)

    def test_chain_and_exact_replay_are_guarded(self):
        s2 = self.coordinator._snapshot_for_feature(self.feature, self.base)
        h1 = self.ledger.snapshot_revalidation_hash(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=self.s1.snapshot_hash, target_snapshot_hash=s2.snapshot_hash, generation=1)
        first = self.ledger.append_feature_snapshot_revalidation(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=self.s1.snapshot_hash, target_snapshot_hash=s2.snapshot_hash, generation=1, revalidation_hash=h1)
        second = self.ledger.append_feature_snapshot_revalidation(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=s2.snapshot_hash, target_snapshot_hash=s2.snapshot_hash, generation=2, revalidation_hash=self.ledger.snapshot_revalidation_hash(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=s2.snapshot_hash, target_snapshot_hash=s2.snapshot_hash, generation=2))
        self.assertEqual(first["generation"], 1); self.assertEqual(second["generation"], 2)
        self.assertEqual(self.ledger.feature_snapshot_authority("F")["generation"], 2)
        with self.assertRaisesRegex(ValueError, "source or generation"):
            self.ledger.append_feature_snapshot_revalidation(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=self.s1.snapshot_hash, target_snapshot_hash="x" * 64, generation=2, revalidation_hash="x" * 64)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.ledger.append_feature_snapshot_revalidation(feature_id="F", feature_contract_hash=self.feature.contract_hash, repository_identity=str(self.repo.resolve()), repo_base_sha=self.base, source_snapshot_hash=s2.snapshot_hash, target_snapshot_hash="y" * 64, generation=3, revalidation_hash="z" * 64)


if __name__ == "__main__":
    unittest.main()
