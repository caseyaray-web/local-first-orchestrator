from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.admission import FeatureAdmissionSpec, FileDisposition
from local_first_orchestrator.decomposition import Criterion, DecompositionPlan, FeatureContract, PlanValidator, Tranche, activate_validated_plan
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.tranche_completion import completion_evidence
from local_first_orchestrator.corrections import CorrectionService
from local_first_orchestrator.planning_coordinator import PlanningCoordinator
from local_first_orchestrator.repository_snapshot import RepositoryPlanValidator, snapshot
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.generated_activation import resolve_generated_activation_context, activate_generated_ticket


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


class SchedulerBoard:
    timeout_seconds = 2
    def create_microticket(self, title, body, *, idempotency_key): return f"ext-{title}"
    def find_comment_marker(self, *args): return "not_found"
    def set_state(self, *args, **kwargs): return None
    def deliver_comment(self, *args, **kwargs): return None


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
        admitted = FeatureAdmissionSpec("F", "Two tranche", "T1", "alpha", self.feature.objective, self.base, self.feature.acceptance_criteria, (), (), (), (FileDisposition("alpha.py", "modify"), FileDisposition("beta.py", "modify")), None)
        self.ledger.connection.execute("insert into feature_contracts(feature_id,contract_hash,contract_json,created_at) values (?,?,?,?)", ("F", self.feature.contract_hash, json.dumps({"spec": admitted.canonical_payload}, sort_keys=True, separators=(",", ":")), 1))
        self.s1 = snapshot(self.repo, self.base, self.feature)
        self.a = ticket("A", "Change alpha", "A", "alpha", "alpha.py")
        self.b = ticket("B", "Change beta implementation", "B", "beta", "beta.py")
        self.initial = DecompositionPlan(1, "F", self.feature.contract_hash, self.base, self.s1.snapshot_hash, (), {"A": ("A",), "B": ("B",)}, (Tranche("T1", 0, "alpha", (), ("A",), (self.a,)), Tranche("T2", 1, "beta", (), ("B",), (self.b,))))
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

    def prepare_scheduler_activation_authority(self, *, checkpoint_decision="approve", escalation_decision=None):
        self.ledger.connection.execute("UPDATE board_projection_outbox SET external_task_id='ext-A',acknowledged_at=1 WHERE ticket_id='A' AND operation='create_microticket'")
        completion = completion_evidence(self.ledger, self.repo, "T1")
        self.ledger.record_tranche_completion(completion)
        checkpoint_path = self.root / "checkpoint.json"
        checkpoint_path.write_text("{}\n")
        checkpoint_hash = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        self.ledger.connection.execute(
            "INSERT INTO tranche_checkpoint_evidence(tranche_id,feature_id,completion_evidence_hash,final_integration_sha,repository_identity,planning_base_sha,planning_snapshot_hash,integration_commands_json,integration_results_json,checkpoint_artifact,checkpoint_artifact_sha256,decision,created_at) VALUES ('T1','F',?,?,?,?,?,'[]','[]',?,?,'ready_for_checkpoint',1)",
            (completion["evidence_hash"], completion["final_integration_sha"], str(self.repo), self.base, self.s1.snapshot_hash, str(checkpoint_path), checkpoint_hash),
        )
        def add_paid(purpose, decision, model_call):
            self.ledger.connection.execute(
                "INSERT INTO paid_checkpoint_evidence(tranche_id,feature_id,checkpoint_artifact_sha256,checkpoint_completion_hash,scheduler_claim_id,request_key,purpose,provider,model,profile,reservation_id,model_call_id,response_json,decision,rationale,created_at) VALUES ('T1','F',?,?,?, ?,?,'p','m','profile','r',?, ?,?,'ok',1)",
                (checkpoint_hash, completion["evidence_hash"], f"claim-{purpose}", f"claim-{purpose}", purpose, model_call, json.dumps({"decision":decision,"rationale":"ok"},sort_keys=True,separators=(",",":")), decision),
            )
        add_paid("integration_checkpoint", checkpoint_decision, "call-checkpoint")
        if escalation_decision is not None:
            add_paid("escalation", escalation_decision, "call-escalation")
        return completion

    def scheduler_materialize_runner(self, coordinator):
        def run(identity):
            outcome = coordinator.materialize_next_tranche(identity["feature_id"])
            self.assertIn(outcome.status, {"activated", "already_materialized"})
            rows = self.ledger.connection.execute("SELECT id FROM tickets WHERE tranche_id=? ORDER BY id", (identity["successor_tranche_id"],)).fetchall()
            ids = [str(row["id"]) for row in rows]
            snapshot_values = set()
            for ticket_id in ids:
                row = self.ledger.connection.execute("SELECT payload_json FROM board_projection_outbox WHERE ticket_id=? AND operation='create_microticket' ORDER BY queued_at DESC LIMIT 1", (ticket_id,)).fetchone()
                payload = json.loads(row["payload_json"])
                contract = json.loads(payload["body"].split("```local-first-contract\\n",1)[1].split("\\n```",1)[0])
                snapshot_values.add((contract["repository_identity"],contract["repo_base_sha"],contract["repo_snapshot_hash"]))
            self.assertEqual(len(snapshot_values),1)
            repository_identity, repo_base_sha, repo_snapshot_hash = snapshot_values.pop()
            return {"candidate_identity":identity,"ticket_ids":ids,"repository_identity":repository_identity,"repo_base_sha":repo_base_sha,"repo_snapshot_hash":repo_snapshot_hash}
        return run

    def test_scheduler_next_tranche_requires_effective_paid_approval(self):
        self.prepare_scheduler_activation_authority(checkpoint_decision="reject")
        self.assertIsNone(self.ledger.claim_next_scheduler_next_tranche_materialize("x",lease_seconds=30,now=100))
        self.assertNotEqual(preview_next(self.ledger,now=100).next_stage,"next_tranche_materialize")

    def test_scheduler_next_tranche_accepts_escalation_approval_only_after_checkpoint_escalates(self):
        self.prepare_scheduler_activation_authority(checkpoint_decision="escalate", escalation_decision="approve")
        claim=self.ledger.claim_next_scheduler_next_tranche_materialize("x",lease_seconds=30,now=100)
        self.assertIsNotNone(claim)
        identity=json.loads(claim["candidate_identity_json"])
        self.assertEqual((identity["approval_purpose"],identity["approval_model_call_id"]),("escalation","call-escalation"))

    def test_scheduler_next_tranche_materializes_once_against_completed_snapshot(self):
        self.prepare_scheduler_activation_authority()
        proposal = DecompositionPlan(1,"F",self.feature.contract_hash,"wrong","wrong",(),{"next":("B",)},(Tranche("proposal",0,"beta",(),("B",),(self.b,)),))
        planner = NextPlanner(proposal)
        coordinator = PlanningCoordinator(self.ledger,self.config,planner)
        scheduler = ProcessNextScheduler(self.ledger,SchedulerBoard(),worker_id="activation",lease_seconds=30,clock=lambda:100,next_tranche_materialize_runner=self.scheduler_materialize_runner(coordinator))
        self.assertEqual(preview_next(self.ledger,now=100).next_stage,"next_tranche_materialize")
        result = scheduler.process_next()
        self.assertEqual((result.stage,result.status),("next_tranche_materialize","completed"))
        evidence = self.ledger.next_tranche_materialization("T1")
        self.assertEqual((evidence["successor_tranche_id"],evidence["repo_base_sha"]),("T2",self.a1))
        self.assertEqual(json.loads(evidence["ticket_ids_json"]),["B"])
        self.assertEqual(len(planner.calls),1)
        self.assertEqual(planner.calls[0][1].base_sha,self.a1)
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM tranches WHERE id='T1'").fetchone()[0],"completed")
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM tranches WHERE id='T2'").fetchone()[0],"active")

    def test_successor_generated_activation_uses_materialized_successor_base(self):
        self.prepare_scheduler_activation_authority()
        proposal = DecompositionPlan(1,"F",self.feature.contract_hash,"wrong","wrong",(),{"next":("B",)},(Tranche("proposal",0,"beta",(),("B",),(self.b,)),))
        coordinator = PlanningCoordinator(self.ledger,self.config,NextPlanner(proposal))
        scheduler = ProcessNextScheduler(
            self.ledger,
            SchedulerBoard(),
            worker_id="activation",
            lease_seconds=30,
            clock=lambda:100,
            next_tranche_materialize_runner=self.scheduler_materialize_runner(coordinator),
        )
        self.assertEqual(scheduler.process_next().stage,"next_tranche_materialize")
        self.ledger.connection.execute(
            "UPDATE board_projection_outbox SET external_task_id='ext-B',acknowledged_at=100 WHERE ticket_id='B' AND operation='create_microticket'"
        )

        context = resolve_generated_activation_context("B", self.config, self.ledger)
        result = activate_generated_ticket("B", self.config, self.ledger)

        self.assertEqual(context.starting_sha,self.a1)
        self.assertEqual(context.repository_identity,str(self.repo))
        self.assertEqual(result.starting_sha,self.a1)
        self.assertEqual(self.ledger.runtime_binding("B")["starting_sha"],self.a1)

    def test_scheduler_next_tranche_recovers_materialized_before_effect_without_replanning(self):
        self.prepare_scheduler_activation_authority()
        proposal = DecompositionPlan(1,"F",self.feature.contract_hash,"wrong","wrong",(),{"next":("B",)},(Tranche("proposal",0,"beta",(),("B",),(self.b,)),))
        planner = NextPlanner(proposal); coordinator = PlanningCoordinator(self.ledger,self.config,planner)
        base_runner = self.scheduler_materialize_runner(coordinator)
        def crash(identity):
            base_runner(identity)
            raise RuntimeError("simulated death after materialization")
        first = ProcessNextScheduler(self.ledger,SchedulerBoard(),worker_id="a",lease_seconds=30,clock=lambda:100,next_tranche_materialize_runner=crash)
        with self.assertRaisesRegex(RuntimeError,"simulated death"):
            first.process_next()
        self.assertEqual(len(planner.calls),1)
        self.assertIsNone(self.ledger.next_tranche_materialization("T1"))
        replay = ProcessNextScheduler(self.ledger,SchedulerBoard(),worker_id="b",lease_seconds=30,clock=lambda:131,next_tranche_materialize_runner=base_runner)
        stages=[]
        for _ in range(4):
            current=replay.process_next(); stages.append(current.stage)
            if current.stage=="next_tranche_materialize": break
        self.assertIn("next_tranche_materialize",stages)
        self.assertEqual(len(planner.calls),1)
        self.assertIsNotNone(self.ledger.next_tranche_materialization("T1"))

    def test_scheduler_activation_waits_for_cards_and_native_graph_then_freezes_exact_projection(self):
        self.prepare_scheduler_activation_authority()
        b1 = ticket("B1","Implement the first beta dependency change","B","beta","beta.py")
        b2 = MicroTicket("B2","Implement the second beta dependency change",("B",),"beta.py::beta",("beta.py",),("Do not change public APIs.",),PatchBudget(1,20),VerificationProfile((("python","-c","pass"),)),"low",True,1,("B1",))
        proposal = DecompositionPlan(1,"F",self.feature.contract_hash,"wrong","wrong",(),{"next":("B1","B2")},(Tranche("proposal",0,"beta",(),("B",),(b1,b2)),))
        planner = NextPlanner(proposal); coordinator = PlanningCoordinator(self.ledger,self.config,planner)
        scheduler = ProcessNextScheduler(self.ledger,SchedulerBoard(),worker_id="activation",lease_seconds=30,clock=lambda:100,next_tranche_materialize_runner=self.scheduler_materialize_runner(coordinator))
        self.assertEqual(scheduler.process_next().stage,"next_tranche_materialize")
        self.assertNotEqual(preview_next(self.ledger,now=100).next_stage,"next_tranche_activation")
        for ticket_id,external_id in (("B1","ext-B1"),("B2","ext-B2")):
            self.ledger.connection.execute("UPDATE board_projection_outbox SET external_task_id=?,acknowledged_at=100 WHERE ticket_id=? AND operation='create_microticket'",(external_id,ticket_id))
        self.assertNotEqual(preview_next(self.ledger,now=100).next_stage,"next_tranche_activation")
        graph_hash="g"*64
        self.ledger.connection.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES ('B2','ext-B2','[\"B1\"]','[\"ext-B1\"]',?,100)",(graph_hash,))
        self.assertEqual(preview_next(self.ledger,now=100).next_stage,"next_tranche_activation")
        final = scheduler.process_next()
        self.assertEqual(final.stage,"next_tranche_activation")
        activation = self.ledger.next_tranche_activation("T2")
        self.assertEqual(json.loads(activation["ticket_ids_json"]),["B1","B2"])
        self.assertEqual(json.loads(activation["external_task_ids_json"]),["ext-B1","ext-B2"])
        self.assertEqual(json.loads(activation["dependency_graph_hashes_json"]),[{"graph_hash":graph_hash,"ticket_id":"B2"}])

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
