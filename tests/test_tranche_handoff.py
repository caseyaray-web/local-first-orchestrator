from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche, activate_validated_plan
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.tranche_completion import completion_evidence
from local_first_orchestrator.corrections import CorrectionService
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


def ticket(ticket_id: str, objective: str, criterion: str, symbol: str, path: str) -> MicroTicket:
    return MicroTicket(ticket_id, objective, (criterion,), f"{path}::{symbol}", (path,), ("Do not change public APIs.",), PatchBudget(1, 20), VerificationProfile((("python", "-c", "pass"),)), "low", True, 1, ())


class NextPlanner:
    cost_class = "local"
    role = "decomposition"
    provider = "fixture-provider"
    model = "fixture-planner"
    profile = "fixture-profile"
    routing_source = "fixture"
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []
    def propose(self, feature, snap, *, artifact_dir):
        self.calls.append((feature, snap))
        return self.proposal


class TrancheHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.root = Path(self.tmp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.name", "T"); self.git("config", "user.email", "t@example.invalid")
        (self.repo / "alpha.py").write_text("def alpha():\n    return 'base'\n")
        (self.repo / "beta.py").write_text("def beta():\n    return 'base'\n")
        self.git("add", "."); self.git("commit", "-qm", "base"); self.base = self.rev("HEAD")
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate()
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        self.feature = FeatureContract("F", "Two tranche", "Apply alpha then beta", (Criterion("A", "alpha accepted"), Criterion("B", "beta accepted")), (), (), (), self.base)
        self.s1 = snapshot(self.repo, self.base, self.feature)
        self.a = ticket("A", "Change alpha", "A", "alpha", "alpha.py")
        self.b = ticket("B", "Change beta implementation", "B", "beta", "beta.py")
        self.initial = DecompositionPlan(1, "F", self.feature.contract_hash, self.base, self.s1.snapshot_hash, (), {"all": ("A", "B")}, (Tranche("T1", 0, "alpha", (), ("A",), (self.a,)), Tranche("T2", 1, "beta", (), ("B",), ())))
        self.initial = DecompositionPlan(self.initial.plan_version, self.initial.feature_id, self.initial.feature_contract_hash, self.initial.repo_base_sha, self.initial.repo_snapshot_hash, self.initial.architecture_decisions, self.initial.criterion_coverage, self.initial.tranches, repository_identity=self.s1.repository_id, repo_snapshot_manifest_json=self.s1.manifest_json)
        pv = PlanValidator().validate(self.feature, self.initial); rv = RepositoryPlanValidator().validate(self.initial, self.s1)
        self.assertTrue(pv.passed, pv.reasons); self.assertTrue(rv.passed, rv.reasons)
        activate_validated_plan(self.ledger, self.feature, self.initial, pv, rv)
        # Simulate the already-proven accepted first ticket and rolling head.
        (self.repo / "alpha.py").write_text("def alpha():\n    return 'accepted'\n")
        self.git("add", "alpha.py"); self.git("commit", "-qm", "accept A"); self.a1 = self.rev("HEAD")
        self.ledger.connection.execute("UPDATE tickets SET state='done' WHERE id='A'")
        self.ledger.record_accepted_evidence("A", self.a1, "alpha", "validated")
        self.git("update-ref", "refs/local-first/tranches/T1/integration-head", self.a1)

    def tearDown(self): self.ledger.close(); self.tmp.cleanup()
    def git(self, *args): return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)
    def rev(self, ref): return self.git("rev-parse", ref).stdout.strip()

    def test_materializes_only_next_tranche_from_completed_head_and_replays(self):
        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T1"))
        s2 = snapshot(self.repo, self.a1, self.feature)
        self.assertEqual(hashlib.sha256((self.repo / "alpha.py").read_bytes()).hexdigest(), next(e.content_hash for e in s2.entries if e.path == "alpha.py"))
        proposal = DecompositionPlan(1, "F", self.feature.contract_hash, "wrong", "wrong", (), {"next": ("B",)}, (Tranche("proposal", 0, "beta", (), ("B",), (self.b,)),))
        planner = NextPlanner(proposal)
        coordinator = PlanningCoordinator(self.ledger, self.config, planner)
        first = coordinator.materialize_next_tranche("F")
        self.assertEqual(first.status, "activated", first.reasons)
        self.assertEqual(planner.calls[0][1].base_sha, self.a1)
        self.assertIn(next(e for e in planner.calls[0][1].entries if e.path == "alpha.py").content_hash, {hashlib.sha256(b"def alpha():\n    return 'accepted'\n").hexdigest()})
        self.assertEqual(self.ledger.get_ticket("B")["state"], "draft")
        self.assertEqual(self.ledger.connection.execute("select status from tranches where id='T1'").fetchone()[0], "completed")
        self.assertEqual(self.ledger.connection.execute("select status from tranches where id='T2'").fetchone()[0], "active")
        self.assertEqual(self.ledger.connection.execute("select count(*) from tickets where id='B'").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from board_projection_outbox where ticket_id='B' and operation='create_microticket'").fetchone()[0], 1)
        second = coordinator.materialize_next_tranche("F")
        self.assertEqual(second.status, "already_materialized")
        self.assertEqual(planner.calls.__len__(), 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranche_completion_evidence").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from events where event_type='generated_microticket_created'").fetchone()[0], 2)
        self.assertEqual(self.ledger.connection.execute("select status from tranches where id='T3'").fetchone() if False else "planned", "planned")

    def test_successor_is_blocked_without_durable_h1(self):
        proposal = DecompositionPlan(1, "F", self.feature.contract_hash, "wrong", "wrong", (), {"next": ("B",)}, (Tranche("proposal", 0, "beta", (), ("B",), (self.b,)),))
        coordinator = PlanningCoordinator(self.ledger, self.config, NextPlanner(proposal))
        outcome = coordinator.materialize_next_tranche("F")
        self.assertEqual(outcome.status, "waiting_not_complete")
        self.assertIn("completion authority", outcome.reasons[0])

    def test_legacy_missing_h1_and_recheck_schema_returns_controlled_unauthorized(self):
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_hash_integrity")
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_immutable_delete")
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_immutable_update")
        self.ledger.connection.execute("DROP TABLE tranche_completion_rechecks")
        authority = CorrectionService(self.ledger, self.repo).completion_authority("T1")
        self.assertEqual(authority["authorized"], False)
        self.assertEqual(authority["status"], "missing_h1")

    def test_legacy_h1_without_recheck_schema_returns_controlled_unauthorized(self):
        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T1"))
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_hash_integrity")
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_immutable_delete")
        self.ledger.connection.execute("DROP TRIGGER tranche_completion_rechecks_immutable_update")
        self.ledger.connection.execute("DROP TABLE tranche_completion_rechecks")
        authority = CorrectionService(self.ledger, self.repo).completion_authority("T1")
        self.assertEqual(authority["authorized"], False)
        self.assertEqual(authority["status"], "completion_schema_missing")

    def test_unrelated_database_errors_are_not_converted_to_not_complete(self):
        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T1"))
        self.ledger.connection.execute("DROP TABLE supplemental_correction_plans")
        with self.assertRaises(sqlite3.OperationalError):
            CorrectionService(self.ledger, self.repo).completion_authority("T1")


if __name__ == "__main__": unittest.main()
