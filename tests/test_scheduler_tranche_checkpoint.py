from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.paid_model import InjectedPaidModelAdapter, PaidInvocationError
from local_first_orchestrator.repository_snapshot import snapshot
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.usage_governor import PaidPurpose, UsageGovernor


class Board:
    timeout_seconds = 2

    def claim_state_projection(self, *args, **kwargs):
        return None

    def claim_evidence_comment(self, *args, **kwargs):
        return None

    def claim_generated_projection(self, *args, **kwargs):
        return None


class SchedulerTrancheCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "base")
        self.base = self.rev("HEAD")
        self.snap = snapshot(self.repo, self.base)

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F','Feature','Objective','active',1,1)"
        )
        self.ledger.connection.execute(
            "INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?,?)",
            (self.base, json.dumps([["python", "-c", "print('integration-ok')"]])),
        )
        self.ledger.connection.execute(
            "INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at,activated_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES ('P','F','fp','{}','active',1,1,?,?,?,?)",
            (str(self.repo), self.base, self.snap.snapshot_hash, self.snap.manifest_json),
        )
        contract = {
            "objective": "finish tranche",
            "criterion_ids": ["AC"],
            "primary_symbol": "app.py::VALUE",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["none"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-c", "pass"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 1,
            "dependencies": [],
        }
        self.ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DONE, contract=contract)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (self.ticket,))
        self.worktree = self.root / "worktree"
        self.git("worktree", "add", "-q", "-b", "lf/T/1", str(self.worktree), self.base)
        (self.worktree / "app.py").write_text("VALUE = 'done'\n", encoding="utf-8")
        self.wgit("add", "app.py")
        self.wgit("commit", "-qm", "accepted")
        self.commit = self.wrev("HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", self.commit)
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,accepted_commit_sha,created_at) VALUES (?,1,?,?,?,?,1)",
            (self.ticket, self.base, "lf/T/1", str(self.worktree), self.commit),
        )
        self.ledger.record_accepted_evidence(self.ticket, self.commit, "diff", "validated")
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        self.controller = LocalFirstController(self.ledger, Board(), self.config)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def git(self, *args: str):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def wgit(self, *args: str):
        return subprocess.run(("git", *args), cwd=self.worktree, text=True, capture_output=True, check=True)

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def wrev(self, ref: str) -> str:
        return self.wgit("rev-parse", ref).stdout.strip()

    def scheduler(self, *, now: int = 100, runner=None) -> ProcessNextScheduler:
        if runner is None:
            runner = lambda tranche_id: self.controller.execute_tranche_checkpoint_only(tranche_id, repository=self.repo)
        return ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="checkpoint",
            lease_seconds=30,
            clock=lambda: now,
            tranche_checkpoint_runner=runner,
        )

    def test_checkpoint_persists_completion_and_artifact_without_activation(self) -> None:
        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id), ("tranche_checkpoint", self.ticket))
        result = self.scheduler().process_next()
        self.assertEqual((result.stage, result.status), ("tranche_checkpoint", "completed"))
        completion = self.ledger.tranche_completion("T")
        checkpoint = self.ledger.tranche_checkpoint("T")
        self.assertEqual(completion["final_integration_sha"], self.commit)
        self.assertEqual(checkpoint["completion_evidence_hash"], completion["evidence_hash"])
        self.assertEqual(checkpoint["decision"], "ready_for_checkpoint")
        self.assertTrue(Path(checkpoint["checkpoint_artifact"]).is_file())
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM tranches WHERE id='T'").fetchone()[0], "active")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM tickets WHERE tranche_id='T'").fetchone()[0], 1)

    def test_completion_ignores_explicit_nonadvancing_duplicate_acceptance(self) -> None:
        from local_first_orchestrator.tranche_completion import completion_evidence
        duplicate = self.ledger.create_ticket(title="duplicate", state=CanonicalState.DONE)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T',created_at=created_at+10 WHERE id=?", (duplicate,))
        self.ledger.record_accepted_evidence(duplicate, self.commit, "duplicate", "validated")
        with self.ledger._transaction() as conn:
            self.ledger._append_event(conn, entity_type="ticket", entity_id=duplicate, event_type="accepted_evidence_recorded", actor_id="controller", payload={"commit_sha": self.commit, "integration_advanced": False})
        evidence = completion_evidence(self.ledger, self.repo, "T")
        self.assertEqual(evidence["accepted_ticket_ids"], [self.ticket])
        self.assertEqual(evidence["accepted_commit_shas"], [self.commit])

    def test_completion_rejects_ambiguous_duplicate_acceptance(self) -> None:
        from local_first_orchestrator.tranche_completion import TrancheNotComplete, completion_evidence
        duplicate = self.ledger.create_ticket(title="duplicate", state=CanonicalState.DONE)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T',created_at=created_at+10 WHERE id=?", (duplicate,))
        self.ledger.record_accepted_evidence(duplicate, self.commit, "duplicate", "validated")
        with self.assertRaisesRegex(TrancheNotComplete, "duplicate accepted commit lacks non-advancing acceptance evidence"):
            completion_evidence(self.ledger, self.repo, "T")

    def test_existing_h1_with_later_accepted_correction_is_not_recheckpointed(self) -> None:
        from local_first_orchestrator.tranche_completion import completion_evidence

        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T"))
        correction = self.ledger.create_ticket(title="correction", state=CanonicalState.DONE, contract={
            "objective": "correct tranche",
            "criterion_ids": ["AC"],
            "primary_symbol": "app.py::VALUE",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["none"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-c", "pass"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 1,
            "dependencies": [],
        })
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (correction,))
        self.git("commit", "--allow-empty", "-qm", "accepted correction")
        correction_commit = self.rev("HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", correction_commit)
        self.ledger.record_accepted_evidence(correction, correction_commit, "diff", "validated")

        preview = preview_next(self.ledger, now=100)

        self.assertNotEqual(preview.next_stage, "tranche_checkpoint")
        self.assertIsNone(self.ledger.claim_next_scheduler_tranche_checkpoint("checkpoint", lease_seconds=30, now=100))
        completion = self.ledger.tranche_completion("T")
        self.assertIsNotNone(completion)
        assert completion is not None
        self.assertEqual(completion["accepted_ticket_ids_json"], json.dumps([self.ticket], separators=(",", ":")))
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

    def test_matching_h1_uses_tranche_base_even_when_active_plan_base_differs(self) -> None:
        from local_first_orchestrator.tranche_completion import completion_evidence

        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T"))
        self.ledger.connection.execute("UPDATE decomposition_plans SET repo_base_sha=? WHERE id='P'", (self.commit,))

        preview = preview_next(self.ledger, now=100)

        self.assertEqual((preview.next_stage, preview.ticket_id), ("tranche_checkpoint", self.ticket))
        self.assertIsNotNone(self.ledger.claim_next_scheduler_tranche_checkpoint("checkpoint", lease_seconds=30, now=100))

    def test_expired_claim_is_durably_failed_when_later_correction_conflicts_with_h1(self) -> None:
        from local_first_orchestrator.tranche_completion import completion_evidence

        self.ledger.record_tranche_completion(completion_evidence(self.ledger, self.repo, "T"))
        claim = self.ledger.claim_next_scheduler_tranche_checkpoint("crashed", lease_seconds=1, now=100)
        self.assertIsNotNone(claim)
        assert claim is not None
        from local_first_orchestrator.corrections import AcceptedPredecessor, CorrectionService, CorrectionTicketSpec, SupplementalCorrectionPlan
        from local_first_orchestrator.ticket import PatchBudget, VerificationProfile

        correction_service = CorrectionService(self.ledger, self.repo)
        correction_plan = correction_service.create_plan(SupplementalCorrectionPlan(
            feature_id="F",
            tranche_id="T",
            source_kind="operator_review",
            source_reference="post-H1-review",
            finding_fingerprint="post-h1-correction",
            finding_summary="accepted evidence requires a supplemental correction",
            predecessors=(AcceptedPredecessor(self.ticket, self.commit),),
            tickets=(CorrectionTicketSpec(
                objective="Correct the accepted tranche evidence.",
                criterion_ids=("AC",),
                primary_symbol="app.py::VALUE",
                allowed_existing_files=("app.py",),
                new_test_files=(),
                forbidden_changes=("none",),
                patch_budget=PatchBudget(max_files=1, max_changed_lines=10),
                verification=VerificationProfile((("python", "-c", "pass"),)),
                risk="low",
                review_required=True,
                max_attempts=1,
                dependencies=(),
                relevant_symbols=("VALUE",),
                acceptance_criteria=("correction is accepted",),
                non_goals=("no unrelated changes",),
                red_evidence="correction evidence fails before repair",
            ),),
            repository_identity=str(self.repo),
            base_sha=self.base,
            snapshot_hash="post-h1-snapshot",
        ))
        correction = correction_service.materialize(correction_plan.correction_plan_id).ticket_id
        self.ledger.connection.execute("UPDATE tickets SET state=? WHERE id=?", (CanonicalState.DONE.value, correction))
        self.git("commit", "--allow-empty", "-qm", "accepted correction")
        correction_commit = self.rev("HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", correction_commit)
        self.ledger.record_accepted_evidence(correction, correction_commit, "diff", "validated")

        preview = preview_next(self.ledger, now=102)
        replay = self.ledger.claim_next_scheduler_tranche_checkpoint("restart", lease_seconds=30, now=102)

        self.assertNotEqual(preview.next_stage, "tranche_checkpoint")
        self.assertIsNone(replay)
        stored = self.ledger.scheduler_claim(str(claim["claim_id"]))
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["last_error"], "tranche checkpoint superseded by post-H1 completion authority")
        self.assertEqual(stored["finalized_at"], 102)
        events = self.ledger.connection.execute(
            "SELECT payload_json FROM events WHERE entity_type='tranche' AND entity_id='T' AND event_type='scheduler_stage_reconciled'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload_json"])["claim_id"], claim["claim_id"])
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

    def test_checkpoint_materializes_final_commit_when_attempt_worktree_is_gone(self) -> None:
        self.git("worktree", "remove", "--force", str(self.worktree))
        self.assertFalse(self.worktree.exists())
        result = self.scheduler().process_next()
        self.assertEqual((result.stage, result.status), ("tranche_checkpoint", "completed"))
        checkpoint = self.ledger.tranche_checkpoint("T")
        self.assertEqual(checkpoint["decision"], "ready_for_checkpoint")
        integration = json.loads(checkpoint["integration_results_json"])
        self.assertEqual(integration[0]["returncode"], 0)
        self.assertFalse((self.root / "artifacts" / "tranches" / "T" / "integration-worktree").exists())

    def test_checkpoint_fails_closed_on_planning_snapshot_corruption(self) -> None:
        self.ledger.connection.execute("UPDATE decomposition_plans SET repo_snapshot_hash=? WHERE id='P'", ("0" * 64,))
        with self.assertRaisesRegex(RuntimeError, "planning snapshot hash drift"):
            self.scheduler().process_next()
        self.assertIsNone(self.ledger.tranche_completion("T"))
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

    def test_integration_failure_is_durable_checkpoint_decision(self) -> None:
        self.ledger.connection.execute(
            "UPDATE tranches SET integration_commands_json=? WHERE id='T'",
            (json.dumps([["python", "-c", "import sys; sys.exit(7)"]]),),
        )
        result = self.scheduler().process_next()
        self.assertEqual(result.stage, "tranche_checkpoint")
        checkpoint = self.ledger.tranche_checkpoint("T")
        self.assertEqual(checkpoint["decision"], "integration_failed")
        integration = json.loads(checkpoint["integration_results_json"])
        self.assertEqual(integration[0]["returncode"], 7)
        self.assertIsNotNone(self.ledger.tranche_completion("T"))

    def test_completed_effect_restarts_without_rerunning_integration(self) -> None:
        claim = self.ledger.claim_next_scheduler_tranche_checkpoint("crashed", lease_seconds=1, now=101)
        self.assertIsNotNone(claim)
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        result = self.controller.execute_tranche_checkpoint_only("T", repository=self.repo)
        applied = self.ledger.apply_scheduler_tranche_checkpoint_effect(claim_id, "crashed", result, now=101)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        calls: list[str] = []
        resumed = self.scheduler(
            now=103,
            runner=lambda tranche_id: calls.append(tranche_id) or (_ for _ in ()).throw(AssertionError("integration must not rerun")),
        )
        final = resumed.process_next()
        self.assertEqual((final.stage, final.status), ("tranche_checkpoint", "completed"))
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        self.assertIsNotNone(self.ledger.tranche_checkpoint("T"))

    def test_artifact_written_before_ledger_apply_replays_without_rerunning_commands(self) -> None:
        counter = self.root / "integration-count.txt"
        script = (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "p.write_text(p.read_text()+'x' if p.exists() else 'x')"
        )
        self.ledger.connection.execute(
            "UPDATE tranches SET integration_commands_json=? WHERE id='T'",
            (json.dumps([["python", "-c", script]]),),
        )

        def crash_after_artifact(tranche_id: str):
            self.controller.execute_tranche_checkpoint_only(tranche_id, repository=self.repo)
            raise RuntimeError("simulated death after checkpoint artifact")

        first = self.scheduler(now=100, runner=crash_after_artifact)
        with self.assertRaisesRegex(RuntimeError, "simulated death after checkpoint artifact"):
            first.process_next()
        self.assertEqual(counter.read_text(), "x")
        self.assertTrue((self.root / "artifacts" / "tranches" / "T" / "checkpoint.json").is_file())
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

        resumed = self.scheduler(now=131)
        result = resumed.process_next()
        self.assertEqual((result.stage, result.status), ("tranche_checkpoint", "completed"))
        self.assertEqual(counter.read_text(), "x")
        self.assertIsNotNone(self.ledger.tranche_checkpoint("T"))

    def _prepare_paid_checkpoint(self) -> None:
        self.assertEqual(self.scheduler().process_next().stage, "tranche_checkpoint")
        self.assertEqual(self.ledger.tranche_checkpoint("T")["decision"], "ready_for_checkpoint")

    def test_paid_checkpoint_approval_is_reserved_and_persisted_once(self) -> None:
        self._prepare_paid_checkpoint()
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls: list[dict[str, object]] = []
        adapter = InjectedPaidModelAdapter(self.ledger, governor, lambda packet: calls.append(packet) or {"decision": "approve", "rationale": "checkpoint is acceptable"})
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="paid",
            lease_seconds=30,
            clock=lambda: 100,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        self.assertEqual(preview_next(self.ledger, now=100).next_stage, "paid_checkpoint")
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.status), ("paid_checkpoint", "completed"))
        evidence = self.ledger.paid_checkpoint("T")
        self.assertEqual(evidence["decision"], "approve")
        self.assertEqual(evidence["request_key"], result.claim_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["completed"], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_calls WHERE feature_id='F' AND purpose='integration_checkpoint'").fetchone()[0], 1)

    def test_paid_checkpoint_escalation_chains_to_one_escalation_call(self) -> None:
        self._prepare_paid_checkpoint()
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=1)
        checkpoint = InjectedPaidModelAdapter(self.ledger, governor, lambda packet: {"decision": "escalate", "rationale": "needs higher-capability review"})
        escalation_calls: list[dict[str, object]] = []
        escalation = InjectedPaidModelAdapter(self.ledger, governor, lambda packet: escalation_calls.append(packet) or {"decision": "approve", "rationale": "escalation approved"})
        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="paid",
            lease_seconds=30,
            clock=lambda: 100,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=checkpoint, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(checkpoint.provider, checkpoint.model, checkpoint.profile),
            paid_escalation_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=escalation, purpose=PaidPurpose.ESCALATION),
            paid_escalation_route=(escalation.provider, escalation.model, escalation.profile),
        )
        self.assertEqual(scheduler.process_next().stage, "paid_checkpoint")
        self.assertEqual(self.ledger.paid_checkpoint("T")["decision"], "escalate")
        self.assertEqual(preview_next(self.ledger, now=100).next_stage, "paid_escalation")
        self.assertEqual(scheduler.process_next().stage, "paid_escalation")
        escalated = self.ledger.paid_checkpoint("T", "escalation")
        self.assertEqual(escalated["decision"], "approve")
        self.assertEqual(len(escalation_calls), 1)
        self.assertEqual(governor.usage("F", PaidPurpose.ESCALATION)["completed"], 1)

    def test_paid_budget_exhaustion_blocks_until_explicit_approval_then_reuses_claim(self) -> None:
        self._prepare_paid_checkpoint()
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=0, escalation=0)
        calls: list[dict[str, object]] = []
        adapter = InjectedPaidModelAdapter(self.ledger, governor, lambda packet: calls.append(packet) or {"decision": "approve", "rationale": "approved after operator budget grant"})
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="paid-a", lease_seconds=30, clock=lambda: 100,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        with self.assertRaisesRegex(PaidInvocationError, "budget exhausted"):
            first.process_next()
        self.assertEqual(calls, [])
        claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE stage='paid_checkpoint'").fetchone()
        self.assertEqual(claim["status"], "claimed")
        original_claim_id = str(claim["claim_id"])
        governor.approve("F", PaidPurpose.INTEGRATION_CHECKPOINT, "operator", "one checkpoint retry", "approval-checkpoint")
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="paid-b", lease_seconds=30, clock=lambda: 131,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        result = resumed.process_next()
        self.assertEqual(result.claim_id, original_claim_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.paid_checkpoint("T")["decision"], "approve")

    def test_unknown_paid_outcome_is_never_reinvoked(self) -> None:
        self._prepare_paid_checkpoint()
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls: list[dict[str, object]] = []
        def fail(packet: dict[str, object]) -> dict[str, object]:
            calls.append(packet)
            raise RuntimeError("transport ambiguous")
        adapter = InjectedPaidModelAdapter(self.ledger, governor, fail)
        first = ProcessNextScheduler(
            self.ledger, Board(), worker_id="paid-a", lease_seconds=30, clock=lambda: 100,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            first.process_next()
        self.assertEqual(len(calls), 1)
        self.assertEqual(governor.usage("F", PaidPurpose.INTEGRATION_CHECKPOINT)["unknown_outcome"], 1)
        resumed = ProcessNextScheduler(
            self.ledger, Board(), worker_id="paid-b", lease_seconds=30, clock=lambda: 131,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        with self.assertRaisesRegex(PaidInvocationError, "unknown"):
            resumed.process_next()
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.ledger.paid_checkpoint("T"))

    def test_completed_paid_call_replays_after_crash_before_scheduler_apply_without_provider_repeat(self) -> None:
        self._prepare_paid_checkpoint()
        governor = UsageGovernor(self.ledger)
        governor.configure("F", architecture=0, checkpoint=1, escalation=0)
        calls: list[dict[str, object]] = []
        adapter = InjectedPaidModelAdapter(
            self.ledger,
            governor,
            lambda packet: calls.append(packet) or {"decision": "approve", "rationale": "durable completed response"},
        )
        claim = self.ledger.claim_next_scheduler_paid_stage(
            "crashed",
            lease_seconds=1,
            purpose=PaidPurpose.INTEGRATION_CHECKPOINT.value,
            provider=adapter.provider,
            model=adapter.model,
            profile=adapter.profile,
            now=101,
        )
        self.assertIsNotNone(claim)
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        paid_result = self.controller.execute_paid_stage_only("T", adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT)
        self.assertEqual(paid_result["decision"], "approve")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.ledger.paid_checkpoint("T"))

        resumed = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="paid-recovery",
            lease_seconds=30,
            clock=lambda: 103,
            paid_checkpoint_runner=lambda tranche_id: self.controller.execute_paid_stage_only(
                tranche_id, adapter=adapter, purpose=PaidPurpose.INTEGRATION_CHECKPOINT
            ),
            paid_checkpoint_route=(adapter.provider, adapter.model, adapter.profile),
        )
        result = resumed.process_next()
        self.assertEqual((result.stage, result.claim_id), ("paid_checkpoint", claim_id))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger.paid_checkpoint("T")["decision"], "approve")


if __name__ == "__main__":
    unittest.main()
