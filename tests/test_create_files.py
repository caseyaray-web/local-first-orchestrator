import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche
from local_first_orchestrator.decomposition_planner import parse
from local_first_orchestrator.readiness import ReadinessError, validate_ticket
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class CreateFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name)
        subprocess.run(("git", "init", "-b", "main"), cwd=self.repo, check=True, capture_output=True)
        subprocess.run(("git", "config", "user.email", "fixture@example.invalid"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Fixture"), cwd=self.repo, check=True)
        (self.repo / "existing.py").write_text("def existing_function():\n    return 1\n")
        (self.repo / "existing_neighbor.py").write_text("def neighbor():\n    return 1\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True); subprocess.run(("git", "commit", "-m", "base"), cwd=self.repo, check=True, capture_output=True)
        self.sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        self.feature = FeatureContract("F", "Create tool", "Add a bounded tool", (Criterion("A", "tool exists"),), (), (), (), self.sha)
        self.snap = snapshot(self.repo, self.sha, self.feature, limit=2, authorized_modify_paths=("existing.py",), authorized_create_paths=("scripts/new-tool.py",))

    def tearDown(self): self.tmp.cleanup()

    def ticket(self, *, allowed=("existing.py",), create=("scripts/new-tool.py",), anchor="existing.py::existing_function", budget=PatchBudget(2, 100)):
        return MicroTicket("T", "Add the bounded tool behavior.", ("A",), anchor, allowed, ("Do not change unrelated behavior.",), budget, VerificationProfile((("python", "-c", "print('ok')"),)), "low", True, 1, (), create_files=create)

    def plan(self, ticket):
        return DecompositionPlan(1, "F", self.feature.contract_hash, self.sha, self.snap.snapshot_hash, (), {"A": ("T",)}, (Tranche("T0", 0, "tool", (), ("A",), (ticket,)),), repository_identity=self.snap.repository_id, repo_snapshot_manifest_json=self.snap.manifest_json)

    def test_mixed_modify_create_passes_all_validation(self):
        ticket = self.ticket(); self.assertIs(validate_ticket(ticket), ticket)
        self.assertTrue(PlanValidator().validate(self.feature, self.plan(ticket)).passed)
        self.assertTrue(RepositoryPlanValidator().validate(self.plan(ticket), self.snap).passed)
        self.assertNotIn("scripts/new-tool.py", {entry.path for entry in self.snap.entries})

    def test_create_only_uses_neighbor_anchor(self):
        self.feature = FeatureContract("F", "Create tool", "Add a neighbor tool", (Criterion("A", "tool exists"),), (), (), (), self.sha)
        self.snap = snapshot(self.repo, self.sha, self.feature, feature_terms=("neighbor",), limit=2, authorized_modify_paths=("existing.py",), authorized_create_paths=("scripts/new-tool.py",))
        ticket = self.ticket(allowed=(), anchor="existing_neighbor.py::neighbor")
        self.assertTrue(RepositoryPlanValidator().validate(self.plan(ticket), self.snap).passed)

    def test_create_in_allowed_and_modify_in_create_reject(self):
        self.assertIn("unknown_file", RepositoryPlanValidator().validate(self.plan(self.ticket(allowed=("scripts/new-tool.py",), create=())), self.snap).reasons)
        self.assertIn("unknown_create_file", RepositoryPlanValidator().validate(self.plan(self.ticket(create=("existing.py",))), self.snap).reasons)

    def test_create_target_does_not_need_symbol_but_future_anchor_rejects(self):
        self.assertTrue(RepositoryPlanValidator().validate(self.plan(self.ticket()), self.snap).passed)
        bad = self.ticket(anchor="scripts/new-tool.py::future")
        self.assertIn("unknown_symbol", RepositoryPlanValidator().validate(self.plan(bad), self.snap).reasons)

    def test_c11_shaped_modify_create_uses_real_pocketbase_anchor(self):
        path = self.repo / "pocketbase/pb_hooks/meal_planner.pb.js"; path.parent.mkdir(parents=True)
        path.write_text("function c07Lifecycle() { return true; }\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True); subprocess.run(("git", "commit", "-m", "add hook"), cwd=self.repo, check=True, capture_output=True)
        sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        feature = FeatureContract("C11", "Meal Planner restore", "Add restore tool", (Criterion("A", "restore"),), (), (), (), sha)
        snap = snapshot(self.repo, sha, feature, limit=1, authorized_modify_paths=("pocketbase/pb_hooks/meal_planner.pb.js",), authorized_create_paths=("scripts/meal-planner-restore.mjs",))
        ticket = self.ticket(allowed=("pocketbase/pb_hooks/meal_planner.pb.js",), create=("scripts/meal-planner-restore.mjs",), anchor="pocketbase/pb_hooks/meal_planner.pb.js::c07Lifecycle")
        plan = DecompositionPlan(1, "C11", feature.contract_hash, sha, snap.snapshot_hash, (), {"A": ("T",)}, (Tranche("T0", 0, "restore", (), ("A",), (ticket,)),), repository_identity=snap.repository_id, repo_snapshot_manifest_json=snap.manifest_json)
        self.assertTrue(RepositoryPlanValidator().validate(plan, snap).passed)

    def test_authorized_non_source_blob_is_reserved_without_symbols(self):
        (self.repo / "docs").mkdir(); (self.repo / "docs/README.md").write_text("run_app documentation\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True); subprocess.run(("git", "commit", "-m", "add docs"), cwd=self.repo, check=True, capture_output=True)
        sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        feature = FeatureContract("F", "Document run_app with existing_function", "Document run_app", (Criterion("A", "document"),), (), (), (), sha)
        snap = snapshot(self.repo, sha, feature, feature_terms=("run_app", "existing_function"), limit=2, authorized_modify_paths=("docs/README.md",))
        entry = next(e for e in snap.entries if e.path == "docs/README.md")
        self.assertEqual(entry.reason, "authorized_modify_target"); self.assertEqual(entry.symbols, ()); self.assertEqual(entry.content_hash, __import__('hashlib').sha256(b"run_app documentation\n").hexdigest())
        self.assertEqual(next(m for m in snap.manifest if m.path == "docs/README.md").kind, "blob")
        self.sha = sha; self.feature = feature; self.snap = snap
        ticket = self.ticket(allowed=("docs/README.md",), create=(), anchor="existing.py::existing_function")
        plan = self.plan(ticket)
        self.assertTrue(RepositoryPlanValidator().validate(plan, snap).passed)

    def test_unauthorized_non_source_blob_is_not_manifest_admitted(self):
        (self.repo / "README.md").write_text("unrelated\n")
        subprocess.run(("git", "add", "."), cwd=self.repo, check=True); subprocess.run(("git", "commit", "-m", "add readme"), cwd=self.repo, check=True, capture_output=True)
        sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()
        snap = snapshot(self.repo, sha, self.feature, limit=2)
        self.assertNotIn("README.md", {m.path for m in snap.manifest})

    def test_duplicate_and_counted_paths(self):
        with self.assertRaisesRegex(ReadinessError, "declared file categories"):
            validate_ticket(self.ticket(allowed=("existing.py",), create=("existing.py",), budget=PatchBudget(2, 100)))
        with self.assertRaises(ReadinessError):
            validate_ticket(self.ticket(budget=PatchBudget(1, 100)))

    def test_parser_emits_and_reads_create_files(self):
        parsed = parse(json.dumps({"plan_version":1,"feature_id":"F","feature_contract_hash":self.feature.contract_hash,"repo_base_sha":self.sha,"repo_snapshot_hash":self.snap.snapshot_hash,"repository_identity":self.snap.repository_id,"repo_snapshot_manifest_json":self.snap.manifest_json,"architecture_decisions":[],"criterion_coverage":{"A":["T"]},"tranches":[{"id":"T0","ordinal":0,"objective":"Add a bounded tool","capabilities":[],"criterion_ids":["A"],"microtickets":[{"ticket_id":"T","objective":"Add the bounded tool behavior.","criterion_ids":["A"],"primary_symbol":"existing.py::existing_function","allowed_files":["existing.py"],"create_files":["scripts/new-tool.py"],"new_test_files":[],"forbidden_changes":["Do not change unrelated behavior."],"patch_budget":{"max_files":2,"max_changed_lines":100},"verification":{"commands":[["python","-c","print('ok')"]]},"risk":"low","review_required":True,"max_attempts":1,"dependencies":[] }]}]}))
        self.assertEqual(parsed.tranches[0].microtickets[0].create_files, ("scripts/new-tool.py",))


if __name__ == "__main__": unittest.main()
