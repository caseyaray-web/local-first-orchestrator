import hashlib
import json
import os
import shutil
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, PlanValidator, Tranche
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class RepositoryProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "a"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def calculate(value):\n    return value\n")
        self._git(self.repo, "init", "-q")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
        self.sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _git(cwd, *args):
        return subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True)

    def _plan(self, snap):
        ticket = MicroTicket("T", "Implement calculation guard.", ("AC-1",), "app.py::calculate", ("app.py",), (), PatchBudget(), VerificationProfile((("python", "-c", "print(1)"),)), "low", True, 1, ())
        feature = type("Feature", (), {})()
        # Repository validation only needs the plan's provenance and active paths.
        return DecompositionPlan(1, "F", "contract", snap.base_sha, snap.snapshot_hash, (), {"scope": ("AC-1",)}, (Tranche("T0", 0, "Implement", (), ("AC-1",), (ticket,)),), repository_identity=snap.repository_id, repo_snapshot_manifest_json=snap.manifest_json)

    def test_same_sha_different_roots_fail_closed(self):
        other = self.root / "b"
        shutil.copytree(self.repo, other)
        snap_a = snapshot(self.repo, self.sha)
        snap_b = snapshot(other, self.sha)
        self.assertEqual(snap_a.base_sha, snap_b.base_sha)
        self.assertNotEqual(snap_a.repository_id, snap_b.repository_id)
        plan = self._plan(snap_a)
        result = RepositoryPlanValidator().validate(plan, snap_b)
        self.assertFalse(result.passed)
        self.assertIn("repository_identity_mismatch", result.reasons)

    def test_symlink_alias_has_same_identity(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.repo, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertEqual(snapshot(self.repo, self.sha).repository_id, snapshot(alias, self.sha).repository_id)

    def test_manifest_is_deterministic_and_recomputable(self):
        first = snapshot(self.repo, self.sha)
        second = snapshot(self.repo, self.sha)
        self.assertEqual(first.manifest_json, second.manifest_json)
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertEqual(hashlib.sha256(json.dumps(json.loads(first.manifest_json), sort_keys=True, separators=(",", ":")).encode()).hexdigest(), first.snapshot_hash)
        self.assertEqual(json.loads(first.manifest_json)["repository_identity"], str(self.repo.resolve()))

    def test_evidence_change_changes_hash(self):
        first = snapshot(self.repo, self.sha, feature_terms=("calculate",))
        (self.repo / "new.py").write_text("def calculate_new(value):\n    return value + 1\n")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "evidence")
        new_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        changed = snapshot(self.repo, new_sha, feature_terms=("calculate",))
        self.assertNotEqual(first.snapshot_hash, changed.snapshot_hash)
        self.assertNotEqual(first.manifest_json, changed.manifest_json)

    def test_tampered_manifest_and_hash_are_rejected(self):
        snap = snapshot(self.repo, self.sha)
        plan = self._plan(snap)
        validator = RepositoryPlanValidator()
        self.assertFalse(validator.validate(replace(plan, repo_snapshot_manifest_json=snap.manifest_json + " "), snap).passed)
        self.assertFalse(validator.validate(replace(plan, repository_identity="/wrong"), snap).passed)
        self.assertIn("repository_identity_mismatch", validator.validate(replace(plan, repository_identity="/wrong"), snap).reasons)

    def test_legacy_schema_rows_remain_readable_without_authority(self):
        db = self.root / "ledger.db"
        ledger = Ledger(db)
        ledger.migrate()
        ledger.connection.execute("INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at) VALUES ('legacy','F','legacy-fp','{}','active',1)")
        ledger.migrate()
        row = ledger.connection.execute("SELECT repository_identity,repo_snapshot_manifest_json FROM decomposition_plans WHERE id='legacy'").fetchone()
        self.assertIsNone(row["repository_identity"])
        self.assertIsNone(row["repo_snapshot_manifest_json"])
        ledger.close()


if __name__ == "__main__":
    unittest.main()
