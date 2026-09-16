from __future__ import annotations

import subprocess
import hashlib
import json
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile


class _Board:
    is_fake = False


class ManualAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.email", "test@example.invalid"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Test"), cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        subprocess.run(("git", "add", "app.py"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.external_id = "ext-task"
        self.worktree = self.repo / ".worktrees" / self.external_id
        self.worktree.parent.mkdir()
        subprocess.run(("git", "worktree", "add", "-q", "-b", f"wt/{self.external_id}", str(self.worktree), self.base), cwd=self.repo, check=True)
        (self.worktree / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        (self.worktree / "test_app.py").write_text("from app import run\nassert run() == 2\n", encoding="utf-8")

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        ticket = MicroTicket(
            "unused", "Change run and verify it.", ("AC-1",), "app.py::run", ("app.py",),
            ("No unrelated files.",), PatchBudget(2, 20),
            VerificationProfile((("python", "test_app.py"),)), "medium", True, 2, (), ("test_app.py",),
        )
        self.ticket_id = self.ledger.create_ticket(
            title="manual adoption", external_id=self.external_id, state=CanonicalState.READY_LOCAL, contract=ticket.contract()
        )
        self.ledger.bind_runtime(self.ticket_id, str(self.repo), self.base)
        self.config = RuntimeConfig(self.repo, self.root / "attempt-worktrees", self.root / "artifacts", repository_allowlist=(self.repo,))
        self.controller = LocalFirstController(self.ledger, _Board(), self.config)
        self.ledger.pause("operator", reason="manual adoption test")
        self.initial_status = subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_adoption_records_truthful_provenance_and_enters_normal_validation(self) -> None:
        adopted = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="reuse verified pre-existing implementation"
        )
        self.assertEqual(adopted["status"], "adopted")
        self.assertEqual(adopted["state"], CanonicalState.IMPLEMENTING.value)
        self.assertEqual(set(adopted["changed_paths"]), {"app.py", "test_app.py"})
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 0)
        stage = self.ledger.model_stage(self.ticket_id, 1, "implementation")
        self.assertEqual(stage["adapter"], "manual-adoption")
        self.assertEqual(stage["base_sha"], self.base)
        self.assertIsNotNone(self.ledger.runtime_stage(self.ticket_id, "manual-adoption-1"))
        after_adoption = subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout
        self.assertEqual(after_adoption, self.initial_status)

        self.ledger.resume("operator", reason="continue deterministic validation")
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.assertIsNotNone(claim)
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "validator")
        result = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(claim["claim_id"]), "validator", result)
        self.assertTrue(result["passed"], result)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.LOCAL_REVIEW.value)
        review_claim = self.ledger.claim_next_scheduler_review("reviewer", lease_seconds=60, review_execution_policy_hash="review-policy")
        self.assertIsNotNone(review_claim)
        candidate = self.ledger.connection.execute("SELECT * FROM review_candidates WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(candidate)
        self.assertTrue(str(candidate["implementation_invocation_id"]).startswith("manual-adoption:"))

    def test_persisted_review_application_includes_approved_untracked_manual_adoption_files(self) -> None:
        adopted = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="candidate with approved untracked test"
        )
        self.ledger.resume("operator", reason="validate candidate before persisted review")
        validation_claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.assertIsNotNone(validation_claim)
        self.ledger.begin_scheduler_claim_effect(str(validation_claim["claim_id"]), "validator")
        validation = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(validation_claim["claim_id"]), "validator", validation)
        self.assertTrue(validation["passed"], validation)
        review_claim = self.ledger.claim_next_scheduler_review("reviewer", lease_seconds=60, review_execution_policy_hash="review-policy")
        self.assertIsNotNone(review_claim)
        candidate = self.ledger.review_candidate(self.ticket_id, 1)
        self.assertIsNotNone(candidate)

        artifact = self.root / "persisted-review.json"
        artifact.write_text(json.dumps({"payload": {"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "verified"}], "findings": [], "suggestions": []}}, sort_keys=True), encoding="utf-8")
        self.assertTrue(self.ledger.record_model_stage(
            self.ticket_id, 1, "review", purpose="review", adapter="test-review",
            request_hash=hashlib.sha256(b"packet").hexdigest(), response_artifact=str(artifact),
            worktree_path="packet-only", base_sha=self.base, diff_hash=str(adopted["diff_hash"]),
        ))
        self.ledger.pause("operator", reason="apply persisted review only")
        applied = self.controller.apply_persisted_review_only(self.ticket_id, 1, repository=self.repo)
        self.assertEqual(applied["verdict"], "pass")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM review_results WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()[0], 1)
        self.assertTrue((self.worktree / "test_app.py").is_file())
        self.assertIn("?? test_app.py", subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout)
        accepted = self.controller.accept_reviewed_candidate_only(self.ticket_id, 1, repository=self.repo)
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD^"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout.strip(), self.base)
        self.assertEqual(accepted["accepted_commit_sha"], subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout.strip())
        committed_files = set(subprocess.run(("git", "diff", "--name-only", self.base, "HEAD"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout.splitlines())
        self.assertEqual(committed_files, {"app.py", "test_app.py"})
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=? AND stage='implementation'", (self.ticket_id,)).fetchone()[0], 0)
        evidence = self.ledger.connection.execute("SELECT diff_summary FROM accepted_evidence WHERE ticket_id=?", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(evidence)
        self.assertIn("manual-adoption:", str(evidence["diff_summary"]))
        self.assertEqual(subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout, "")
        self.ledger.connection.execute("insert into features(id,title,status,created_at,updated_at) values ('manual-feature','manual','active',0,0)")
        self.ledger.connection.execute("insert into tranches(id,feature_id,ordinal,status,base_sha) values ('manual-tranche','manual-feature',0,'active',?)", (self.base,))
        self.ledger.connection.execute("update tickets set feature_id='manual-feature', tranche_id='manual-tranche' where id=?", (self.ticket_id,))
        subprocess.run(("git", "update-ref", "refs/local-first/tranches/manual-tranche/integration-head", self.base), cwd=self.repo, check=True)
        integrated = self.controller.integrate_accepted_candidate_only(self.ticket_id, 1, repository=self.repo)
        self.assertEqual(integrated["status"], "integrated")
        self.assertEqual(integrated["integration_head"], accepted["accepted_commit_sha"])
        self.assertEqual(subprocess.run(("git", "rev-parse", "refs/local-first/tranches/manual-tranche/integration-head"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(), accepted["accepted_commit_sha"])

    def test_started_review_claim_can_fail_and_be_reissued_under_new_policy(self) -> None:
        self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="candidate for review retry"
        )
        self.ledger.resume("operator", reason="validate review retry candidate")
        validation_claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.assertIsNotNone(validation_claim)
        self.ledger.begin_scheduler_claim_effect(str(validation_claim["claim_id"]), "validator")
        validation = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(validation_claim["claim_id"]), "validator", validation)
        self.assertTrue(validation["passed"], validation)

        first = self.ledger.claim_next_scheduler_review("reviewer-old", lease_seconds=60, review_execution_policy_hash="old-policy")
        self.assertIsNotNone(first)
        self.ledger.begin_scheduler_claim_effect(str(first["claim_id"]), "reviewer-old")
        self.ledger.start_model_invocation(invocation_id="inflight-review", ticket_id=self.ticket_id, attempt_number=1, stage="review", provider="provider", model="model", packet_hash="packet", worktree_path="packet-only", timeout_seconds=60)
        with self.assertRaisesRegex(RuntimeError, "active or recoverable"):
            self.ledger.fail_started_review_claim(str(first["claim_id"]), "reviewer-old", error="must not retire active invocation")
        self.ledger.finish_model_invocation("inflight-review", status="process_error", duration_seconds=0.1, error={"type": "test"})
        failed = self.ledger.fail_started_review_claim(str(first["claim_id"]), "reviewer-old", error="provider override rejected before review output")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(self.ledger.connection.execute("SELECT 1 FROM review_candidates WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone())

        second = self.ledger.claim_next_scheduler_review("reviewer-new", lease_seconds=60, review_execution_policy_hash="new-policy")
        self.assertIsNotNone(second)
        self.assertNotEqual(second["claim_id"], first["claim_id"])
        identity = __import__("json").loads(str(second["candidate_identity_json"]))
        self.assertNotEqual(identity["review_policy_hash"], __import__("json").loads(str(first["candidate_identity_json"]))["review_policy_hash"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims WHERE ticket_id=? AND stage='review:1'", (self.ticket_id,)).fetchone()[0], 1)
        event = self.ledger.connection.execute("SELECT payload_json FROM events WHERE entity_id=? AND event_type='scheduler_review_claim_failed' ORDER BY id DESC LIMIT 1", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(event)
        self.assertIn(str(first["claim_id"]), str(event["payload_json"]))
        reissued = self.ledger.connection.execute("SELECT payload_json FROM events WHERE entity_id=? AND event_type='scheduler_stage_reissued' ORDER BY id DESC LIMIT 1", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(reissued)
        self.assertIn(str(first["claim_id"]), str(reissued["payload_json"]))
        self.assertIn("previous_candidate_identity_json", str(reissued["payload_json"]))

    def test_failed_manual_validation_can_reconcile_and_adopt_corrected_attempt_two(self) -> None:
        (self.worktree / "app.py").write_text("def run():\n    return 2\n\ndef extra():\n    return 3\n", encoding="utf-8")
        first = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="first candidate"
        )
        self.assertEqual(first["attempt_number"], 1)
        self.ledger.resume("operator", reason="validate first candidate")
        first_claim = self.ledger.claim_next_scheduler_validation("validator-1", lease_seconds=60)
        self.assertIsNotNone(first_claim)
        self.ledger.begin_scheduler_claim_effect(str(first_claim["claim_id"]), "validator-1")
        failed = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(first_claim["claim_id"]), "validator-1", failed)
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE scheduler_stage_claims SET lease_expires_at=0 WHERE claim_id=?", (str(first_claim["claim_id"]),))
        self.assertFalse(failed["passed"], failed)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.VERIFYING.value)
        self.ledger.pause("operator", reason="reconcile failed manual validation")
        reconciled = self.ledger.reconcile_failed_attempt(
            self.ticket_id,
            operator_id="operator",
            classification="validation_failure",
            retry_base_sha=self.base,
            runtime_identity={"source": "manual-adoption-test"},
            forensic_artifact_paths=(first["implementation_artifact"], failed["validation_artifact"]),
        )
        self.assertEqual(reconciled["prospective_next_attempt_number"], 2)
        self.assertFalse(reconciled["cleanup_required"])
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.READY_LOCAL.value)
        self.assertEqual(self.ledger.connection.execute("SELECT outcome FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()[0], "failed_retired")

        (self.worktree / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        second = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="corrected candidate"
        )
        self.assertEqual(second["attempt_number"], 2)
        self.assertNotEqual(second["diff_hash"], first["diff_hash"])
        first_attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()
        second_attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=2", (self.ticket_id,)).fetchone()
        self.assertEqual(second_attempt["pre_diff_hash"], first_attempt["post_diff_hash"])
        self.assertEqual(second_attempt["base_sha"], self.base)
        self.assertEqual(self.ledger.runtime_stage(self.ticket_id, "implementation_completed")["attempt_number"], 2)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 0)

        self.ledger.resume("operator", reason="validate corrected candidate")
        second_claim = self.ledger.claim_next_scheduler_validation("validator-2", lease_seconds=60)
        self.assertIsNotNone(second_claim)
        self.ledger.begin_scheduler_claim_effect(str(second_claim["claim_id"]), "validator-2")
        passed = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(second_claim["claim_id"]), "validator-2", passed)
        self.assertTrue(passed["passed"], passed)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.LOCAL_REVIEW.value)
        self.assertEqual(self.ledger.runtime_stage(self.ticket_id, "validation_completed")["attempt_number"], 2)

    def test_manual_validation_reconciliation_requires_completed_scheduler_effect(self) -> None:
        (self.worktree / "app.py").write_text("def run():\n    return 2\n\ndef extra():\n    return 3\n", encoding="utf-8")
        first = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="candidate with scope failure"
        )
        self.ledger.resume("operator", reason="run validation without completing claim effect")
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.assertIsNotNone(claim)
        failed = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.assertFalse(failed["passed"], failed)
        self.ledger.pause("operator", reason="attempt unsafe reconciliation")
        with self.assertRaisesRegex(ValueError, "valid durable evidence"):
            self.ledger.reconcile_failed_attempt(
                self.ticket_id, operator_id="operator", classification="validation_failure", retry_base_sha=self.base,
                runtime_identity={"source": "test"}, forensic_artifact_paths=(first["implementation_artifact"], failed["validation_artifact"]),
            )
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.VERIFYING.value)

    def test_manual_retry_respects_max_attempts(self) -> None:
        (self.worktree / "app.py").write_text("def run():\n    return 2\n\ndef extra():\n    return 3\n", encoding="utf-8")
        first = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="first candidate"
        )
        self.ledger.resume("operator", reason="validate first candidate")
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "validator")
        failed = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(claim["claim_id"]), "validator", failed)
        self.ledger.pause("operator", reason="reconcile first candidate")
        self.ledger.reconcile_failed_attempt(
            self.ticket_id, operator_id="operator", classification="validation_failure", retry_base_sha=self.base,
            runtime_identity={"source": "test"}, forensic_artifact_paths=(first["implementation_artifact"], failed["validation_artifact"]),
        )
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE tickets SET max_attempts=1 WHERE id=?", (self.ticket_id,))
        (self.worktree / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "max_attempts"):
            self.controller.adopt_existing_implementation(
                self.ticket_id, repository=self.repo, operator_id="operator", reason="should exceed budget"
            )
        self.assertIsNone(self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number=2", (self.ticket_id,)).fetchone())

    def test_manual_retry_uses_reconciled_base_and_rejects_drift(self) -> None:
        (self.worktree / "app.py").write_text("def run():\n    return 2\n\ndef extra():\n    return 3\n", encoding="utf-8")
        first = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="first candidate"
        )
        self.ledger.resume("operator", reason="validate first candidate")
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60)
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "validator")
        failed = self.controller.execute_deterministic_validation_only(self.ticket_id, repository=self.repo)
        self.ledger.complete_scheduler_validation_effect(str(claim["claim_id"]), "validator", failed)
        self.ledger.pause("operator", reason="reconcile against moved base")
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        subprocess.run(("git", "add", "later.txt"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "later base"), cwd=self.repo, check=True)
        later_base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.ledger.reconcile_failed_attempt(
            self.ticket_id, operator_id="operator", classification="validation_failure", retry_base_sha=later_base,
            runtime_identity={"source": "test"}, forensic_artifact_paths=(first["implementation_artifact"], failed["validation_artifact"]),
        )
        (self.worktree / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "authoritative execution base"):
            self.controller.adopt_existing_implementation(
                self.ticket_id, repository=self.repo, operator_id="operator", reason="must not silently rebase"
            )
        self.assertIsNone(self.ledger.connection.execute("SELECT 1 FROM attempts WHERE ticket_id=? AND attempt_number=2", (self.ticket_id,)).fetchone())

    def test_adoption_requires_pause(self) -> None:
        self.ledger.resume("operator", reason="test refusal")
        with self.assertRaisesRegex(PermissionError, "paused"):
            self.controller.adopt_existing_implementation(
                self.ticket_id, repository=self.repo, operator_id="operator", reason="should fail"
            )

    def test_adoption_rejects_out_of_scope_untracked_file(self) -> None:
        (self.worktree / "extra.txt").write_text("nope\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unapproved untracked"):
            self.controller.adopt_existing_implementation(
                self.ticket_id, repository=self.repo, operator_id="operator", reason="should fail"
            )
        after_rejection = subprocess.run(("git", "status", "--porcelain=v1", "--untracked-files=all"), cwd=self.worktree, text=True, capture_output=True, check=True).stdout
        self.assertIn("?? extra.txt", after_rejection)
        self.assertNotIn(" A test_app.py", after_rejection)

    def test_exact_orphan_artifact_is_replayable_after_crash(self) -> None:
        artifact = self.root / "artifacts" / self.ticket_id / "1" / "manual-adoption.json"
        with mock.patch.object(self.ledger, "record_manual_implementation_adoption", side_effect=RuntimeError("simulated crash after artifact write")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.controller.adopt_existing_implementation(
                    self.ticket_id, repository=self.repo, operator_id="operator", reason="replayable orphan"
                )
        self.assertTrue(artifact.is_file())
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.READY_LOCAL.value)
        adopted = self.controller.adopt_existing_implementation(
            self.ticket_id, repository=self.repo, operator_id="operator", reason="replayable orphan"
        )
        self.assertEqual(adopted["status"], "adopted")

    def test_conflicting_orphan_artifact_fails_closed(self) -> None:
        artifact = self.root / "artifacts" / self.ticket_id / "1" / "manual-adoption.json"
        with mock.patch.object(self.ledger, "record_manual_implementation_adoption", side_effect=RuntimeError("simulated crash after artifact write")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.controller.adopt_existing_implementation(
                    self.ticket_id, repository=self.repo, operator_id="operator", reason="conflicting orphan"
                )
        artifact.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "artifact conflict"):
            self.controller.adopt_existing_implementation(
                self.ticket_id, repository=self.repo, operator_id="operator", reason="conflicting orphan"
            )


if __name__ == "__main__":
    unittest.main()
