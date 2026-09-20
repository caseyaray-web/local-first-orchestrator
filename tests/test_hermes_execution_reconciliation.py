from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig, _isolated_candidate_diff
from local_first_orchestrator.execution_handoff import HANDOFF_MARKER, HANDOFF_SENTINEL
from local_first_orchestrator.hermes_board import ExternalExecutionRun, ExternalExecutionSnapshot, ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState


class ExecutionBoard:
    timeout_seconds = 1

    def __init__(self, snapshot: ExternalExecutionSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def execution_snapshot(self, task_id: str) -> ExternalExecutionSnapshot:
        self.calls += 1
        if task_id != self.snapshot.task.id:
            raise KeyError(task_id)
        return self.snapshot

    def find_comment_marker(self, *args, **kwargs): return "not_found"
    def deliver_comment(self, *args, **kwargs): return None
    def create_microticket(self, *args, **kwargs): return "H-generated"
    def set_state(self, *args, **kwargs): return None
    def get_task(self, task_id: str): return self.snapshot.task

    def reopen_review_handoff(self, task_id: str, *, reason: str):
        self.reopened = (task_id, reason)
        return ExternalTicket(task_id, "external", HANDOFF_MARKER, "ready", str(Path(self.snapshot.task.workspace_path or ".")))

    def create_repair_task(self, predecessor_task_id: str, *, title: str, body: str, workspace_path, downstream_child_ids, idempotency_key: str, reason: str):
        self.repair_created = {
            "predecessor_task_id": predecessor_task_id,
            "title": title,
            "body": body,
            "workspace_path": workspace_path,
            "downstream_child_ids": downstream_child_ids,
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
        return ExternalTicket("H-retry", title, body, "blocked", str(Path(self.snapshot.task.workspace_path or ".")), parents=(predecessor_task_id,))

    def activate_repair_task(self, task_id: str, *, reason: str):
        self.repair_activated = (task_id, reason)
        return ExternalTicket(task_id, "retry", HANDOFF_MARKER, "ready", None)


class RecoveryReviewModel:
    provider = "fixture-provider"
    model = "fixture-review-model"

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.artifact_path = artifact_dir / f"{purpose}-result.json"
        payload = {
            "verdict": "pass",
            "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "corrected validator"}],
            "findings": [],
            "suggestions": [],
        }
        self.artifact_path.write_text(
            json.dumps({"provider": self.provider, "model": self.model, "payload": payload}, sort_keys=True),
            encoding="utf-8",
        )
        return type("ReviewResult", (), {"payload": payload, "artifact_path": self.artifact_path})()


class RecoveryFailingReviewModel(RecoveryReviewModel):
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict

    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.artifact_path = artifact_dir / f"{purpose}-result.json"
        payload = {
            "verdict": self.verdict,
            "criterion_results": [{"criterion_id": "AC-1", "status": "fail", "evidence": "review found a bounded defect"}],
            "findings": [{"severity": "blocking", "criterion_id": "AC-1", "file": "app.py", "symbol": "value", "evidence": "review found a bounded defect", "minimal_repair": "fix value", "verification": "run configured test", "fingerprint_input": f"recovered review {self.verdict}"}],
            "suggestions": [],
        }
        self.artifact_path.write_text(json.dumps({"provider": self.provider, "model": self.model, "payload": payload}, sort_keys=True), encoding="utf-8")
        return type("ReviewResult", (), {"payload": payload, "artifact_path": self.artifact_path})()


class HermesExecutionReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(("git", "init", "-q", str(self.repo)), check=True)
        subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "Test"), cwd=self.repo, check=True)
        (self.repo / "app.py").write_text('def value():\n    return "old"\n')
        subprocess.run(("git", "add", "app.py"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "base"), cwd=self.repo, check=True)
        self.base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        contract = {
            "objective": "change value",
            "criterion_ids": ["AC-1"],
            "primary_symbol": "app.py::value",
            "allowed_files": ["app.py"],
            "forbidden_changes": [],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-m", "py_compile", "app.py"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 2,
            "dependencies": [],
        }
        self.ticket_id = self.ledger.create_ticket(title="external", external_id="H-1", state=CanonicalState.READY_LOCAL, contract=contract)
        self.ledger.bind_runtime(self.ticket_id, str(self.repo), self.base)
        (self.repo / "app.py").write_text('def value():\n    return "new"\n')
        self.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "scheduled", str(self.repo)),
            session_id="session-1",
            branch_name="worker-branch",
            started_at=10,
            completed_at=20,
            runs=(ExternalExecutionRun(7, "completed", "completed", 10, 20, "worker completed", "worker-code", 123, {"source": "dispatcher"}),),
        )
        self.board = ExecutionBoard(self.snapshot)
        self.controller = LocalFirstController(
            self.ledger,
            self.board,
            RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", repository_allowlist=(self.repo,)),
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_completed_hermes_run_becomes_one_attempt_and_validation_candidate(self) -> None:
        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["attempt_number"], 1)
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["branch_name"], "worker-branch")
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.IMPLEMENTING.value)
        stage = self.ledger.model_stage(self.ticket_id, 1, "implementation")
        self.assertIsNotNone(stage)
        self.assertEqual(stage["adapter"], "hermes-dispatch")
        self.assertEqual(stage["worktree_path"], str(self.repo.resolve()))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 0)
        self.assertTrue(Path(stage["response_artifact"]).is_file())

        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=30, now=100)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["ticket_id"], self.ticket_id)
        self.assertEqual(claim["stage"], "validation:1")

    def test_authorized_untracked_file_is_bound_into_candidate_fingerprint(self) -> None:
        self.ledger.connection.execute(
            "UPDATE tickets SET create_files_json=? WHERE id=?",
            ('["new.py"]', self.ticket_id),
        )
        (self.repo / "new.py").write_text('VALUE = "new file"\n')

        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)

        recorded = str(result["diff_hash"])
        status = subprocess.run(("git", "status", "--short", "--", "new.py"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(status, "?? new.py")
        frozen = _isolated_candidate_diff(self.repo, self.base, allowed_new_paths=("new.py",))
        self.assertEqual(recorded, frozen["diff_hash"])
        self.assertIn("new.py", str(frozen["diff"]))
        (self.repo / "new.py").write_text('VALUE = "changed after freeze"\n')
        changed = _isolated_candidate_diff(self.repo, self.base, allowed_new_paths=("new.py",))
        self.assertNotEqual(recorded, changed["diff_hash"])

    def test_binary_new_file_hash_is_identical_before_and_after_commit(self) -> None:
        payload = bytes(range(256)) * 4
        (self.repo / "blob.bin").write_bytes(payload)
        before = _isolated_candidate_diff(self.repo, self.base, allowed_new_paths=("blob.bin",))
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "binary candidate"), cwd=self.repo, check=True)
        head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
        after = _isolated_candidate_diff(self.repo, self.base, target_sha=head)
        self.assertEqual(before["diff_hash"], after["diff_hash"])
        self.assertEqual(before["diff"], after["diff"])
        self.assertIn("GIT binary patch", str(before["diff"]))

    def test_unauthorized_untracked_file_fails_before_attempt_creation(self) -> None:
        (self.repo / "rogue.txt").write_text("unexpected\n")
        with self.assertRaisesRegex(RuntimeError, "unauthorized untracked content"):
            self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM hermes_execution_reconciliations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 0)

    def test_replay_returns_same_attempt_without_duplicate_stage(self) -> None:
        first = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        second = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        self.assertEqual(first["attempt_number"], second["attempt_number"])
        self.assertEqual(second["status"], "already_reconciled")
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_stage_artifacts WHERE ticket_id=? AND stage='implementation'", (self.ticket_id,)).fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM hermes_execution_reconciliations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 1)

    def test_local_first_projection_run_is_not_accepted_as_worker_execution(self) -> None:
        self.board.snapshot = ExternalExecutionSnapshot(
            task=self.snapshot.task,
            session_id=None,
            branch_name=None,
            started_at=10,
            completed_at=20,
            runs=(ExternalExecutionRun(8, "completed", "completed", 10, 20, "local-first projection ticket-event:1", None, None, None),),
        )
        with self.assertRaisesRegex(RuntimeError, "no completed Hermes worker run"):
            self.controller.reconcile_hermes_execution("H-1")
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)

    def test_premature_hermes_done_fails_closed_before_attempt_creation(self) -> None:
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "done", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=self.snapshot.runs,
        )
        with self.assertRaisesRegex(RuntimeError, "completion_authority_bypassed"):
            self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM hermes_execution_reconciliations").fetchone()[0], 0)

    def test_generated_owned_review_with_no_diff_reopens_same_card_without_local_attempt(self) -> None:
        self._make_generated_owned()
        subprocess.run(("git", "checkout", "--", "app.py"), cwd=self.repo, check=True)
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", HANDOFF_MARKER, "review", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=(ExternalExecutionRun(7, "review", "review_requested", 10, 20, "ready", "worker-code", None, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7, require_handoff=True)

        self.assertEqual(result["status"], "implementation_reopened")
        self.assertEqual(result["external_task_id"], "H-1")
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)
        self.assertEqual(self.board.reopened[0], "H-1")

    def test_generated_owned_done_with_no_diff_activates_replacement_without_local_attempt(self) -> None:
        self._make_generated_owned()
        subprocess.run(("git", "checkout", "--", "app.py"), cwd=self.repo, check=True)
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", HANDOFF_MARKER, "done", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=(ExternalExecutionRun(7, "done", "completed", 10, 20, "worker completed", "worker-code", None, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7, require_handoff=True)

        self.assertEqual(result["status"], "replacement_activated")
        self.assertEqual(result["external_task_id"], "H-retry")
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)
        self.assertEqual(self.ledger.resolve_external_task_id(self.ticket_id), "H-retry")
        self.assertIsNone(self.board.repair_created["workspace_path"])
        self.assertEqual(self.board.repair_activated[0], "H-retry")
        retry_worktree = self.repo / ".worktrees" / "H-retry"
        self.assertTrue(retry_worktree.is_dir())
        retry_head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=retry_worktree, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(retry_head, self.base)
        stage = self.ledger.connection.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage='generated-repair-activation-1'", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(stage)
        stage_detail = json.loads(stage["detail"])
        self.assertEqual(stage_detail["recovery_kind"], "premature_done_no_diff")
        self.assertEqual(stage_detail["base_sha"], self.base)
        self.assertEqual(stage_detail["workspace_path"], str(retry_worktree.resolve()))

    def test_generated_owned_review_handoff_reconciles_for_local_validation(self) -> None:
        self._make_generated_owned()
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", HANDOFF_MARKER, "review", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=(ExternalExecutionRun(7, "review", "review_requested", 10, 20, "implementation ready for Local First validation", "worker-code", None, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7, require_handoff=True)

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["attempt_number"], 1)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.IMPLEMENTING.value)
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=30, now=100)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["ticket_id"], self.ticket_id)

    def test_generated_owned_done_with_handoff_marker_reconciles_for_local_validation(self) -> None:
        self._make_generated_owned()
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", HANDOFF_MARKER, "done", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=(ExternalExecutionRun(7, "done", "completed", 10, 20, "worker completed; awaiting Local First validation", "worker-code", None, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7, require_handoff=True)

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["attempt_number"], 1)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.IMPLEMENTING.value)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM hermes_execution_reconciliations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 1)

    def test_generated_owned_done_without_handoff_marker_still_fails_closed(self) -> None:
        self._make_generated_owned()
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "done", str(self.repo)),
            session_id=self.snapshot.session_id,
            branch_name=self.snapshot.branch_name,
            started_at=self.snapshot.started_at,
            completed_at=self.snapshot.completed_at,
            runs=self.snapshot.runs,
        )
        with self.assertRaisesRegex(RuntimeError, "completion_authority_bypassed"):
            self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7, require_handoff=True)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 0)

    def _make_generated_owned(self) -> None:
        self.ledger.connection.execute("UPDATE tickets SET external_id=NULL WHERE id=?", (self.ticket_id,))
        event_id = self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,'generated_microticket_created','controller','test','{}',1) RETURNING id",
            (self.ticket_id,),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id,acknowledged_at) VALUES (?,?,'draft','{}','generated-owned',1,'create_microticket','H-1',1)",
            (self.ticket_id, event_id),
        )

    def test_generated_hermes_owned_ticket_is_never_locally_claimable(self) -> None:
        self._make_generated_owned()
        self.assertIsNone(self.ledger.claim_next_scheduler_implementation("local-worker", lease_seconds=30, now=100))

    def _seed_generated_repair_attempt_two(self) -> None:
        self._make_generated_owned()
        self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        stage = self.ledger.model_stage(self.ticket_id, 1, "implementation")
        self.assertIsNotNone(stage)
        self.ledger.connection.execute("UPDATE tickets SET state=? WHERE id=?", (CanonicalState.REPAIRING.value, self.ticket_id))
        detail = json.dumps({"action": "repair", "attempt_number": 1, "next_attempt_number": 2, "failure_evidence": "fix restore parity"}, sort_keys=True, separators=(",", ":"))
        self.ledger.record_runtime_stage(self.ticket_id, "repair-routing-1", detail, attempt_number=1)
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,created_at) VALUES (?,?,?,?,?,?,?)",
            (self.ticket_id, 2, self.base, "worker-branch", str(self.repo.resolve()), str(stage["diff_hash"]), 30),
        )

    def test_repair_discovery_hides_reconciled_run_until_generated_repair_activation(self) -> None:
        self._seed_generated_repair_attempt_two()
        self.assertEqual(self.ledger.hermes_execution_candidates(), [])
        candidates = self.ledger.generated_repair_activation_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["ticket_id"], self.ticket_id)
        self.assertEqual(int(candidates[0]["attempt_number"]), 2)

        self.ledger.record_runtime_stage(
            self.ticket_id,
            "generated-repair-activation-2",
            json.dumps({"ticket_id": self.ticket_id, "attempt_number": 2, "external_task_id": "H-2", "predecessor_external_task_id": "H-1"}, sort_keys=True, separators=(",", ":")),
            attempt_number=2,
            base_sha=self.base,
        )
        visible = self.ledger.hermes_execution_candidates()
        self.assertEqual([row["ticket_id"] for row in visible], [self.ticket_id])
        self.assertEqual([row["external_task_id"] for row in visible], ["H-2"])
        self.assertEqual(self.ledger.resolve_external_task_id(self.ticket_id), "H-2")

    def test_fresh_hermes_repair_run_completes_preallocated_attempt_two(self) -> None:
        self._seed_generated_repair_attempt_two()
        self.ledger.record_runtime_stage(
            self.ticket_id,
            "generated-repair-activation-2",
            json.dumps({"ticket_id": self.ticket_id, "attempt_number": 2, "external_task_id": "H-2", "predecessor_external_task_id": "H-1"}, sort_keys=True, separators=(",", ":")),
            attempt_number=2,
            base_sha=self.base,
        )
        (self.repo / "app.py").write_text('def value():\n    return "repaired"\n')
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-2", "external repair", HANDOFF_MARKER, "blocked", str(self.repo), parents=("H-1",)),
            session_id="session-2",
            branch_name="worker-branch",
            started_at=30,
            completed_at=40,
            runs=(ExternalExecutionRun(8, "blocked", "blocked", 30, 40, HANDOFF_SENTINEL, "worker-code", 456, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-2", hermes_run_id=8, require_handoff=True)

        self.assertEqual(result["attempt_number"], 2)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 2)
        attempt = self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=2", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(attempt["post_diff_hash"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id=? AND attempt_number=3", (self.ticket_id,)).fetchone()[0], 0)

    def test_completed_dispatch_validation_can_replay_after_proven_validator_defect(self) -> None:
        reconciled = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60, now=100, ticket_id=self.ticket_id)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "validator", now=100)
        identity = json.loads(str(claim["candidate_identity_json"]))
        false_artifact = self.root / "false-validation.json"
        false_artifact.write_text(json.dumps({"errors": ["obsolete validator false positive"]}, sort_keys=True), encoding="utf-8")
        false_sha = hashlib.sha256(false_artifact.read_bytes()).hexdigest()
        false_result = {
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": "obsolete validator false positive",
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "review_diff": "unused",
            "review_selected_files": {},
        }
        self.assertTrue(self.ledger.record_runtime_stage(
            self.ticket_id,
            "validation-1",
            json.dumps(false_result, sort_keys=True),
            attempt_number=1,
            artifact_path=str(false_artifact),
            artifact_sha256=false_sha,
            base_sha=self.base,
        ))
        self.assertTrue(self.ledger.record_runtime_stage(
            self.ticket_id,
            "validation_completed",
            json.dumps(false_result, sort_keys=True),
            attempt_number=1,
            artifact_path=str(false_artifact),
            artifact_sha256=false_sha,
            base_sha=self.base,
        ))
        claim_result = {
            "ticket_id": self.ticket_id,
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": false_result["compact_evidence"],
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "replayed": False,
        }
        self.ledger.complete_scheduler_validation_effect(str(claim["claim_id"]), "validator", claim_result, now=101)
        self.ledger.complete_scheduler_claim(str(claim["claim_id"]), "validator", claim_result, now=101)
        self.ledger.record_terminal_unresolvable(
            self.ticket_id,
            attempt_number=1,
            failure_fingerprint="f" * 64,
            reason="attempt limit reached after deterministic validation failure",
            summary={"deterministic_failure": {"source": "validation", "attempt_number": 1, "failure_evidence": "obsolete validator false positive"}},
            notification_target="mattermost:ops",
        )
        self.ledger.pause("operator", reason="recover corrected validator")

        recovered = self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id,
            1,
            repository=self.repo,
            operator_id="operator",
            reason="validator false positive corrected",
        )

        self.assertEqual(recovered["state"], CanonicalState.LOCAL_REVIEW.value)
        self.assertIn("validation passed", str(recovered["preflight_compact_evidence"]))
        replay_claim = self.ledger.scheduler_claim(str(claim["claim_id"]))
        self.assertEqual(replay_claim["status"], "completed")
        self.assertIsNotNone(replay_claim["side_effect_completed_at"])
        self.assertIsNotNone(replay_claim["finalized_at"])
        self.assertEqual(json.loads(str(replay_claim["result_json"]))["passed"], True)
        self.assertIsNotNone(self.ledger.runtime_stage(self.ticket_id, "validation-controller-defect-archive-1-1"))
        self.assertIsNotNone(self.ledger.runtime_stage(self.ticket_id, "validation-completed-controller-defect-archive-1-1"))
        current_validation = self.ledger.runtime_stage(self.ticket_id, "validation-1")
        self.assertIsNotNone(current_validation)
        self.assertEqual(json.loads(str(current_validation["detail"]))["passed"], True)
        terminal = self.ledger.connection.execute("SELECT resolved_at FROM terminal_ticket_failures WHERE ticket_id=?", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(terminal["resolved_at"])
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)
        replayed = self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id,
            1,
            repository=self.repo,
            operator_id="operator",
            reason="validator false positive corrected",
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)
        self.assertEqual(reconciled["diff_hash"], identity["implementation_diff_hash"])

    def test_recovered_validation_replays_fresh_review_routing_after_stale_triage(self) -> None:
        reconciled = self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60, now=100, ticket_id=self.ticket_id)
        self.assertIsNotNone(claim)
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(str(claim["claim_id"]), "validator", now=100)
        identity = json.loads(str(claim["candidate_identity_json"]))
        false_artifact = self.root / "false-validation-recovery.json"
        false_artifact.write_text(json.dumps({"errors": ["obsolete validator false positive"]}, sort_keys=True), encoding="utf-8")
        false_sha = hashlib.sha256(false_artifact.read_bytes()).hexdigest()
        candidate_diff = str(_isolated_candidate_diff(self.repo, self.base)["diff"])
        false_result = {
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": "obsolete validator false positive",
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "review_diff": candidate_diff,
            "review_selected_files": {"app.py": candidate_diff},
        }
        self.ledger.record_runtime_stage(
            self.ticket_id,
            "validation-1",
            json.dumps(false_result, sort_keys=True),
            attempt_number=1,
            artifact_path=str(false_artifact),
            artifact_sha256=false_sha,
            base_sha=self.base,
        )
        self.ledger.record_runtime_stage(
            self.ticket_id,
            "validation_completed",
            json.dumps(false_result, sort_keys=True),
            attempt_number=1,
            artifact_path=str(false_artifact),
            artifact_sha256=false_sha,
            base_sha=self.base,
        )
        claim_result = {
            "ticket_id": self.ticket_id,
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": false_result["compact_evidence"],
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "replayed": False,
        }
        self.ledger.complete_scheduler_validation_effect(str(claim["claim_id"]), "validator", claim_result, now=101)
        self.ledger.complete_scheduler_claim(str(claim["claim_id"]), "validator", claim_result, now=101)
        self.ledger.connection.execute("UPDATE tickets SET max_attempts=1 WHERE id=?", (self.ticket_id,))
        routing_claim = self.ledger.claim_next_scheduler_repair_routing("router", lease_seconds=60, now=110, ticket_id=self.ticket_id)
        self.assertIsNotNone(routing_claim)
        assert routing_claim is not None
        routing_id = str(routing_claim["claim_id"])
        proposed = self.ledger.plan_scheduler_repair_routing_effect(routing_id, "router", now=110)
        self.ledger.record_runtime_stage(
            self.ticket_id,
            "triage-feedback-1",
            json.dumps({
                "ticket_id": self.ticket_id,
                "attempt_number": 1,
                "failure_fingerprint": proposed["failure_fingerprint"],
                "failure_evidence": proposed["failure_evidence"],
                "feedback": "obsolete validator false positive was escalated",
            }, sort_keys=True, separators=(",", ":")),
            attempt_number=1,
        )
        self.ledger.begin_scheduler_claim_effect(routing_id, "router", now=110)
        routed = self.ledger.apply_scheduler_repair_routing_effect(routing_id, "router", now=111)
        self.ledger.complete_scheduler_claim(routing_id, "router", json.loads(str(routed["result_json"])), now=111)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.NEEDS_TRIAGE.value)
        self.assertEqual(json.loads(str(self.ledger.runtime_stage(self.ticket_id, "repair-routing-1")["detail"]))["action"], "triage")
        self.ledger.record_terminal_unresolvable(
            self.ticket_id,
            attempt_number=1,
            failure_fingerprint=str(proposed["failure_fingerprint"]),
            reason="attempt limit reached after deterministic validation failure",
            summary={"deterministic_failure": {"source": "validation", "attempt_number": 1, "failure_evidence": "obsolete validator false positive"}},
            notification_target="mattermost:ops",
        )
        self.ledger.pause("operator", reason="recover corrected validator")
        recovered = self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id,
            1,
            repository=self.repo,
            operator_id="operator",
            reason="validator false positive corrected",
        )
        self.assertEqual(recovered["state"], CanonicalState.LOCAL_REVIEW.value)
        archive_rows = self.ledger.connection.execute(
            "SELECT archive_kind,source_claim_id,source_runtime_stage FROM validation_recovery_archives WHERE ticket_id=? AND attempt_number=1 ORDER BY archive_kind",
            (self.ticket_id,),
        ).fetchall()
        self.assertEqual([row["archive_kind"] for row in archive_rows], ["repair_routing_claim", "repair_routing_stage", "triage_feedback_stage"])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims WHERE ticket_id=? AND stage='repair_routing:1'", (self.ticket_id,)).fetchone()[0], 0)
        self.assertIsNone(self.ledger.runtime_stage(self.ticket_id, "repair-routing-1"))
        self.assertIsNone(self.ledger.runtime_stage(self.ticket_id, "triage-feedback-1"))
        self.ledger.resume("operator", reason="run temporary recovery regression")

        review_model = RecoveryReviewModel()
        review_controller = LocalFirstController(self.ledger, self.board, self.controller.config, local_model=review_model)
        scheduler = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="reviewer",
            target_ticket_id=self.ticket_id,
            lease_seconds=60,
            clock=lambda: 200,
            review_runner=lambda ticket_id: review_controller.execute_fresh_review_only(ticket_id, repository=self.repo),
            review_execution_policy_hash=review_controller.review_execution_policy_hash(),
            acceptance_runner=lambda ticket_id: review_controller.inspect_acceptance_candidate_only(ticket_id, repository=self.repo),
        )
        for _ in range(4):
            if preview_next(self.ledger, now=200).next_stage == "review":
                break
            projection_result = scheduler.process_next()
            self.assertIn(projection_result.stage, {"state_projection", "evidence_comment"})
        else:
            self.fail("temporary recovery did not reach fresh review")
        self.assertEqual(preview_next(self.ledger, now=200).next_stage, "review")

        review_result = scheduler.process_next()
        self.assertEqual((review_result.status, review_result.stage, review_result.ticket_id), ("completed", "review", self.ticket_id))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM review_results WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()[0], 0)
        self.assertEqual(preview_next(self.ledger, now=200).next_stage, "repair_routing")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims WHERE ticket_id=? AND stage='repair_routing:1'", (self.ticket_id,)).fetchone()[0], 0)
        routed_review = scheduler.process_next()
        self.assertEqual((routed_review.status, routed_review.stage, routed_review.ticket_id), ("completed", "repair_routing", self.ticket_id))
        review_row = self.ledger.connection.execute("SELECT verdict FROM review_results WHERE ticket_id=? AND attempt_number=1", (self.ticket_id,)).fetchone()
        self.assertEqual(review_row["verdict"], "pass")
        routing_detail = json.loads(str(self.ledger.runtime_stage(self.ticket_id, "repair-routing-1")["detail"]))
        self.assertEqual((routing_detail["source"], routing_detail["action"]), ("review", "pass"))
        self.assertEqual(preview_next(self.ledger, now=200).next_stage, "acceptance")
        acceptance_result = scheduler.process_next()
        self.assertEqual((acceptance_result.status, acceptance_result.stage, acceptance_result.ticket_id), ("completed", "acceptance", self.ticket_id))
        accepted = self.ledger.accepted_candidate(self.ticket_id)
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted["candidate_fingerprint"], identity["implementation_diff_hash"])
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.ACCEPTED.value)
        self.assertEqual(reconciled["diff_hash"], identity["implementation_diff_hash"])

        def integrate(ticket_id: str) -> dict[str, object]:
            claim = self.ledger.connection.execute("SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage LIKE 'git_integration:%' AND status='claimed'", (ticket_id,)).fetchone()
            assert claim is not None
            candidate_identity = json.loads(str(claim["candidate_identity_json"]))
            self.ledger.start_git_commit_intent(ticket_id, candidate_identity, now=201)
            subprocess.run(("git", "add", "app.py"), cwd=self.repo, check=True)
            subprocess.run(("git", "commit", "-qm", str(candidate_identity["commit_message"])), cwd=self.repo, check=True)
            commit_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()
            return {"candidate_identity": candidate_identity, "commit_sha": commit_sha, "integration_head_before": str(candidate_identity["base_sha"]), "integration_head_after": commit_sha}

        integration_scheduler = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="integrator",
            target_ticket_id=self.ticket_id,
            lease_seconds=60,
            clock=lambda: 201,
            git_integration_runner=integrate,
        )
        integrated = None
        for _ in range(5):
            candidate = integration_scheduler.process_next()
            if candidate.stage == "git_integration":
                integrated = candidate
                break
            self.assertIn(candidate.stage, {"state_projection", "evidence_comment"})
        self.assertIsNotNone(integrated)
        assert integrated is not None
        self.assertEqual((integrated.status, integrated.stage, integrated.ticket_id), ("completed", "git_integration", self.ticket_id))
        completed = None
        for _ in range(5):
            candidate = integration_scheduler.process_next()
            if candidate.stage == "completion":
                completed = candidate
                break
            self.assertIn(candidate.stage, {"state_projection", "evidence_comment"})
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual((completed.status, completed.stage, completed.ticket_id), ("completed", "completion", self.ticket_id))
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.DONE.value)
        self.assertIsNotNone(self.ledger.connection.execute("SELECT 1 FROM git_commit_evidence WHERE ticket_id=?", (self.ticket_id,)).fetchone())

    def test_validation_recovery_archive_schema_migrates_idempotently_and_is_immutable(self) -> None:
        self.ledger.migrate()
        self.ledger.connection.execute(
            "INSERT INTO validation_recovery_archives(archive_id,ticket_id,attempt_number,terminal_generation,archive_kind,operator_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("schema-archive", self.ticket_id, 1, 1, "repair_routing_claim", "operator", "schema test", 1),
        )
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM validation_recovery_archives WHERE archive_id='schema-archive'").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE validation_recovery_archives SET reason='changed' WHERE archive_id='schema-archive'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("DELETE FROM validation_recovery_archives WHERE archive_id='schema-archive'")

    def _prepare_stale_validation_recovery(self, *, with_triage_claim: bool = False) -> dict[str, object]:
        """Build one temporary, production-shaped stale-routing recovery case."""
        self.controller.reconcile_hermes_execution("H-1", hermes_run_id=7)
        validation_claim = self.ledger.claim_next_scheduler_validation("validator", lease_seconds=60, now=100, ticket_id=self.ticket_id)
        self.assertIsNotNone(validation_claim)
        assert validation_claim is not None
        validation_claim_id = str(validation_claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(validation_claim_id, "validator", now=100)
        identity = json.loads(str(validation_claim["candidate_identity_json"]))
        false_artifact = self.root / "matrix-false-validation.json"
        false_artifact.write_text(json.dumps({"errors": ["obsolete validator false positive"]}, sort_keys=True), encoding="utf-8")
        false_sha = hashlib.sha256(false_artifact.read_bytes()).hexdigest()
        false_result = {
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": "obsolete validator false positive",
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "review_diff": str(_isolated_candidate_diff(self.repo, self.base)["diff"]),
            "review_selected_files": {"app.py": str(_isolated_candidate_diff(self.repo, self.base)["diff"])},
        }
        self.ledger.record_runtime_stage(self.ticket_id, "validation-1", json.dumps(false_result, sort_keys=True), attempt_number=1, artifact_path=str(false_artifact), artifact_sha256=false_sha, base_sha=self.base)
        self.ledger.record_runtime_stage(self.ticket_id, "validation_completed", json.dumps(false_result, sort_keys=True), attempt_number=1, artifact_path=str(false_artifact), artifact_sha256=false_sha, base_sha=self.base)
        validation_claim_result = {
            "ticket_id": self.ticket_id,
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": false_result["compact_evidence"],
            "validation_artifact": str(false_artifact),
            "validation_artifact_sha256": false_sha,
            "replayed": False,
        }
        self.ledger.complete_scheduler_validation_effect(validation_claim_id, "validator", validation_claim_result, now=101)
        self.ledger.complete_scheduler_claim(validation_claim_id, "validator", validation_claim_result, now=101)
        self.ledger.connection.execute("UPDATE tickets SET max_attempts=1 WHERE id=?", (self.ticket_id,))
        routing_claim = self.ledger.claim_next_scheduler_repair_routing("router", lease_seconds=60, now=110, ticket_id=self.ticket_id)
        self.assertIsNotNone(routing_claim)
        assert routing_claim is not None
        routing_claim_id = str(routing_claim["claim_id"])
        proposed = self.ledger.plan_scheduler_repair_routing_effect(routing_claim_id, "router", now=110)
        self.ledger.begin_scheduler_claim_effect(routing_claim_id, "router", now=110)
        routed = self.ledger.apply_scheduler_repair_routing_effect(routing_claim_id, "router", now=111)
        routing_result = json.loads(str(routed["result_json"]))
        self.ledger.complete_scheduler_claim(routing_claim_id, "router", routing_result, now=111)
        self.ledger.record_terminal_unresolvable(
            self.ticket_id,
            attempt_number=1,
            failure_fingerprint=str(proposed["failure_fingerprint"]),
            reason="attempt limit reached after deterministic validation failure",
            summary={"deterministic_failure": {"source": "validation", "attempt_number": 1, "failure_evidence": false_result["compact_evidence"]}},
            notification_target="mattermost:ops",
        )
        if with_triage_claim:
            policy_hash = "triage-policy-for-matrix"
            ticket = self.ledger.get_ticket(self.ticket_id)
            triage_identity = self.ledger._triage_claim_identity(ticket, attempt_number=1, failure_evidence=str(routing_result["failure_evidence"]), triage_execution_policy_hash=policy_hash)
            encoded_identity = json.dumps(triage_identity, sort_keys=True, separators=(",", ":"))
            triage_result = {"ticket_id": self.ticket_id, "attempt_number": 1, "candidate_identity": triage_identity}
            now = 112
            self.ledger.connection.execute(
                "INSERT INTO scheduler_stage_claims(claim_id,ticket_id,stage,status,lease_owner,lease_expires_at,attempt_count,result_json,side_effect_started_at,side_effect_completed_at,finalized_at,candidate_identity_json,created_at,updated_at) VALUES (?,?,?,'completed',NULL,NULL,1,?,?,?,?,?,?,?)",
                ("matrix-triage-claim", self.ticket_id, "triage:1", json.dumps(triage_result, sort_keys=True, separators=(",", ":")), now, now, now, encoded_identity, now, now),
            )
        self.ledger.pause("operator", reason="matrix recovery")
        return {"routing_result": routing_result, "routing_claim_id": routing_claim_id, "triage_policy_hash": "triage-policy-for-matrix"}

    def _recovery_state_snapshot(self) -> dict[str, object]:
        tables = {
            "ticket": "SELECT * FROM tickets WHERE id=?",
            "claims": "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? ORDER BY claim_id",
            "stages": "SELECT * FROM runtime_stages WHERE ticket_id=? ORDER BY stage",
            "archives": "SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id",
            "events": "SELECT * FROM events WHERE entity_type='ticket' AND entity_id=? ORDER BY id",
            "terminal": "SELECT * FROM terminal_ticket_failures WHERE ticket_id=?",
        }
        snapshot: dict[str, object] = {}
        for name, query in tables.items():
            rows = self.ledger.connection.execute(query, (self.ticket_id,)).fetchall()
            snapshot[name] = [tuple(row) for row in rows]
        return snapshot

    def _attempt_matrix_recovery(self) -> dict[str, object]:
        return self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id,
            1,
            repository=self.repo,
            operator_id="operator",
            reason="matrix validator correction",
        )

    def test_recovery_rejects_runtime_route_attempt_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        self.ledger.connection.execute("UPDATE runtime_stages SET attempt_number=2 WHERE ticket_id=? AND stage='repair-routing-1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "routing lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_triage_canonical_identity_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=? WHERE claim_id='matrix-triage-claim'", (json.dumps({"ticket_id": self.ticket_id, "attempt_number": 1, "parent_depth": 999, "unresolved_criteria": [], "failure_evidence_hash": "x", "ticket_policy_hash": "x", "triage_execution_policy_hash": "triage-policy-for-matrix"}, sort_keys=True, separators=(",", ":")),))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "triage lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_triage_failure_evidence_hash_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        claim = self.ledger.connection.execute("SELECT candidate_identity_json FROM scheduler_stage_claims WHERE claim_id='matrix-triage-claim'").fetchone()
        identity = json.loads(str(claim["candidate_identity_json"]))
        identity["failure_evidence_hash"] = "0" * 64
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=?,result_json=? WHERE claim_id='matrix-triage-claim'", (encoded, json.dumps({"candidate_identity": identity}, sort_keys=True, separators=(",", ":"))))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "triage lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_cross_ticket_route_evidence_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        route = self.ledger.connection.execute("SELECT detail FROM runtime_stages WHERE ticket_id=? AND stage='repair-routing-1'", (self.ticket_id,)).fetchone()
        detail = json.loads(str(route["detail"]))
        detail["ticket_id"] = "other-ticket"
        self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='repair-routing-1'", (json.dumps(detail, sort_keys=True, separators=(",", ":")), self.ticket_id))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "routing lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_leased_triage_claim_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET lease_owner='leased',lease_expires_at=999 WHERE claim_id='matrix-triage-claim'")
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "active or incomplete downstream triage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_malformed_route_json_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        self.ledger.connection.execute("UPDATE runtime_stages SET detail='{bad json' WHERE ticket_id=? AND stage='repair-routing-1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "routing is malformed"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_partial_route_claim_and_stage_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        self.ledger.connection.execute("DELETE FROM runtime_stages WHERE ticket_id=? AND stage='repair-routing-1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "lineage is incomplete"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_archive_insertion_rolls_back_before_release(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        before = self._recovery_state_snapshot()
        self.ledger.failure_injector = lambda point: (_ for _ in ()).throw(RuntimeError("injected archive failure")) if point == "after_validation_recovery_downstream_archive" else None
        with self.assertRaisesRegex(RuntimeError, "injected archive failure"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_completed_dependent_triage_claim_is_archived_with_canonical_identity(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        recovered = self._attempt_matrix_recovery()
        self.assertEqual(recovered["archived_downstream_identities"], [str(row[0]) for row in self.ledger.connection.execute("SELECT archive_id FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_kind", (self.ticket_id,)).fetchall()])
        kinds = [row[0] for row in self.ledger.connection.execute("SELECT archive_kind FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_kind", (self.ticket_id,)).fetchall()]
        self.assertEqual(kinds, ["repair_routing_claim", "repair_routing_stage", "triage_claim"])
        self.assertIsNone(self.ledger.connection.execute("SELECT 1 FROM scheduler_stage_claims WHERE ticket_id=? AND stage='triage:1'", (self.ticket_id,)).fetchone())
        archived = self.ledger.connection.execute("SELECT source_claim_result_json,source_claim_snapshot_json FROM validation_recovery_archives WHERE ticket_id=? AND archive_kind='triage_claim'", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(archived)
        assert archived is not None
        result_identity = json.loads(str(archived["source_claim_result_json"]))["candidate_identity"]
        snapshot_identity = json.loads(str(json.loads(str(archived["source_claim_snapshot_json"]))["candidate_identity_json"]))
        self.assertEqual(result_identity, snapshot_identity)

    def test_exact_recovery_replay_keeps_downstream_archives_byte_identical(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self._attempt_matrix_recovery()
        before = [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id", (self.ticket_id,)).fetchall()]
        replayed = self.controller.recover_terminal_validation_controller_defect(self.ticket_id, 1, repository=self.repo, operator_id="operator", reason="matrix validator correction")
        after = [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id", (self.ticket_id,)).fetchall()]
        self.assertTrue(replayed["replayed"])
        self.assertEqual(before, after)

    def _run_recovered_review_variant(self, *, verdict: str, max_attempts: int) -> dict[str, object]:
        self._prepare_stale_validation_recovery()
        self._attempt_matrix_recovery()
        self.ledger.connection.execute("UPDATE tickets SET max_attempts=? WHERE id=?", (max_attempts, self.ticket_id))
        self.ledger.resume("operator", reason="run recovered review variant")
        review_model = RecoveryFailingReviewModel(verdict)
        review_controller = LocalFirstController(self.ledger, self.board, self.controller.config, local_model=review_model)  # type: ignore[arg-type]
        scheduler = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="recovered-reviewer",
            target_ticket_id=self.ticket_id,
            lease_seconds=60,
            clock=lambda: 200,
            review_runner=lambda ticket_id: review_controller.execute_fresh_review_only(ticket_id, repository=self.repo),
            review_execution_policy_hash=review_controller.review_execution_policy_hash(),
        )
        for _ in range(4):
            if preview_next(self.ledger, now=200).next_stage == "review":
                break
            projection = scheduler.process_next()
            self.assertIn(projection.stage, {"state_projection", "evidence_comment"})
        review_preview = preview_next(self.ledger, now=200)
        self.assertEqual((review_preview.next_stage, review_preview.ticket_id), ("review", self.ticket_id))
        review_result = scheduler.process_next()
        self.assertEqual((review_result.status, review_result.stage), ("completed", "review"))
        routing_preview = preview_next(self.ledger, now=200)
        self.assertEqual((routing_preview.next_stage, routing_preview.ticket_id), ("repair_routing", self.ticket_id))
        routing_result = scheduler.process_next()
        self.assertEqual((routing_result.status, routing_result.stage), ("completed", "repair_routing"))
        route = self.ledger.runtime_stage(self.ticket_id, "repair-routing-1")
        self.assertIsNotNone(route)
        assert route is not None
        return json.loads(str(route["detail"]))

    def test_recovered_failing_review_routes_repair_without_archived_identity_collision(self) -> None:
        decision = self._run_recovered_review_variant(verdict="repair", max_attempts=2)
        self.assertEqual((decision["source"], decision["action"]), ("review", "repair"))
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.REPAIRING.value)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM validation_recovery_archives WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 2)

    def test_recovered_failing_review_routes_triage_without_archived_identity_collision(self) -> None:
        decision = self._run_recovered_review_variant(verdict="escalate", max_attempts=1)
        self.assertEqual((decision["source"], decision["action"]), ("review", "triage"))
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.NEEDS_TRIAGE.value)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM validation_recovery_archives WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 2)

    def test_recovery_rejects_route_claim_result_disagreement_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        claim = self.ledger.connection.execute("SELECT result_json FROM scheduler_stage_claims WHERE ticket_id=? AND stage='repair_routing:1'", (self.ticket_id,)).fetchone()
        result = json.loads(str(claim["result_json"]))
        result["failure_evidence"] = "different routing evidence"
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET result_json=? WHERE ticket_id=? AND stage='repair_routing:1'", (json.dumps(result, sort_keys=True, separators=(",", ":")), self.ticket_id))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "downstream routing"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_active_route_claim_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET lease_owner='leased',lease_expires_at=999 WHERE ticket_id=? AND stage='repair_routing:1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "active or incomplete downstream routing"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_malformed_triage_claim_json_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json='{bad json' WHERE claim_id='matrix-triage-claim'")
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "downstream triage is malformed"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_migrates_actual_legacy_ledger_without_rewriting_rows(self) -> None:
        legacy_path = self.root / "legacy-pre-recovery.db"
        legacy = Ledger(legacy_path)
        legacy.migrate()
        legacy.connection.execute("INSERT INTO tickets(id,title,state,created_at,updated_at) VALUES ('legacy-ticket','legacy','draft',1,1)")
        legacy.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_update")
        legacy.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_delete")
        legacy.connection.execute("DROP TABLE validation_recovery_archives")
        legacy.close()
        migrated = Ledger(legacy_path)
        migrated.migrate()
        self.assertEqual(migrated.connection.execute("SELECT title FROM tickets WHERE id='legacy-ticket'").fetchone()[0], "legacy")
        self.assertIsNotNone(migrated.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='validation_recovery_archives'").fetchone())
        migrated.close()

    def test_terminal_retry_reuses_preallocated_attempt_without_fresh_repair_route(self) -> None:
        self._seed_generated_repair_attempt_two()
        self.ledger.connection.execute(
            "UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='repair-routing-1'",
            (json.dumps({"action": "triage", "attempt_number": 1}, sort_keys=True, separators=(",", ":")), self.ticket_id),
        )
        self.ledger.record_runtime_stage(
            self.ticket_id,
            "generated-repair-activation-2",
            json.dumps({"ticket_id": self.ticket_id, "attempt_number": 2, "external_task_id": "H-2", "predecessor_external_task_id": "H-1"}, sort_keys=True, separators=(",", ":")),
            attempt_number=2,
            base_sha=self.base,
        )
        (self.repo / "app.py").write_text('def value():\n    return "repaired after terminal retry"\n')
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-2", "external repair", HANDOFF_MARKER, "blocked", str(self.repo), parents=("H-1",)),
            session_id="session-2",
            branch_name="worker-branch",
            started_at=30,
            completed_at=40,
            runs=(ExternalExecutionRun(8, "blocked", "blocked", 30, 40, HANDOFF_SENTINEL, "worker-code", 456, {"source": "dispatcher"}),),
        )

        result = self.controller.reconcile_hermes_execution("H-2", hermes_run_id=8, require_handoff=True)

        self.assertEqual(result["attempt_number"], 2)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 2)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM attempts WHERE ticket_id=? AND attempt_number=3", (self.ticket_id,)).fetchone()[0], 0)

    def test_prepare_stops_when_git_directory_is_replaced(self) -> None:
        original_git = self.repo / ".git-original"
        swapped = False
        real_run = subprocess.run

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            if not swapped and "rev-parse" in command and "refs/heads/wt/H-1" in command:
                swapped = True
                (self.repo / ".git").rename(original_git)
                replacement_path = self.root / "git-replacement"
                real_run(("git", "init", "-q", str(replacement_path)), check=True)
                replacement = replacement_path / ".git"
                replacement.rename(self.repo / ".git")
            return real_run(*args, **kwargs)

        with patch("local_first_orchestrator.controller.subprocess.run", side_effect=swapping_run):
            with self.assertRaisesRegex(RuntimeError, "identity|STOP|reconciliation"):
                self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        self.assertTrue(swapped)
        self.assertFalse((self.repo / ".worktrees" / "H-1").exists())

    def test_prepare_stops_when_worktrees_parent_is_replaced(self) -> None:
        original_parent = self.repo / ".worktrees-original"
        swapped = False
        real_run = subprocess.run

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            if not swapped and "rev-parse" in command and "refs/heads/wt/H-1" in command:
                swapped = True
                (self.repo / ".worktrees").rename(original_parent)
                (self.repo / ".worktrees").mkdir()
            return real_run(*args, **kwargs)

        with patch("local_first_orchestrator.controller.subprocess.run", side_effect=swapping_run):
            with self.assertRaisesRegex(RuntimeError, "identity|STOP|reconciliation"):
                self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        self.assertTrue(swapped)
        self.assertFalse((self.repo / ".worktrees" / "H-1").exists())

    def test_prepare_stops_when_repository_is_swapped_between_branch_check_and_add(self) -> None:
        replacement = self.root / "replacement"
        shutil.copytree(self.repo, replacement)
        original = self.root / "original"
        swapped = False
        real_run = subprocess.run

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            if (not swapped and "rev-parse" in command and "refs/heads/wt/H-1" in command):
                swapped = True
                self.repo.rename(original)
                replacement.rename(self.repo)
            return real_run(*args, **kwargs)

        with patch("local_first_orchestrator.controller.subprocess.run", side_effect=swapping_run):
            with self.assertRaisesRegex(RuntimeError, "identity|STOP|reconciliation"):
                self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")

        self.assertTrue(swapped)
        self.assertFalse((self.repo / ".worktrees" / "H-1").exists())
        self.assertEqual(
            real_run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(),
            self.base,
        )
        self.assertFalse(self.ledger.connection.execute("SELECT 1 FROM events WHERE event_type LIKE '%worktree%'").fetchone())

    def test_prepare_creation_stops_on_metadata_child_copy_swap_after_branch_validation(self) -> None:
        real_run = subprocess.run
        swapped = False
        child = self.repo / ".git" / "worktrees" / "H-1"

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            result = real_run(*args, **kwargs)
            if not swapped and "branch" in command and "--show-current" in command:
                swapped = True
                replacement = self.root / "metadata-copy-after-branch"
                shutil.copytree(child, replacement)
                child.rename(self.root / "metadata-original-after-branch")
                replacement.rename(child)
            return result

        with patch("local_first_orchestrator.controller.subprocess.run", side_effect=swapping_run):
            with self.assertRaisesRegex(RuntimeError, "identity|STOP|reconciliation"):
                self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        self.assertTrue(swapped)

    def test_prepare_replay_stops_on_metadata_child_copy_swap_after_status_validation(self) -> None:
        self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        real_run = subprocess.run
        swapped = False
        child = self.repo / ".git" / "worktrees" / "H-1"

        def swapping_run(*args, **kwargs):
            nonlocal swapped
            command = tuple(args[0] if args else (kwargs.get("args") or ()))
            result = real_run(*args, **kwargs)
            if not swapped and "status" in command and "--porcelain=v1" in command:
                swapped = True
                replacement = self.root / "metadata-copy-after-status"
                shutil.copytree(child, replacement)
                child.rename(self.root / "metadata-original-after-status")
                replacement.rename(child)
            return result

        with patch("local_first_orchestrator.controller.subprocess.run", side_effect=swapping_run):
            with self.assertRaisesRegex(RuntimeError, "identity|STOP|reconciliation"):
                self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        self.assertTrue(swapped)

    def test_prepare_hermes_dispatch_worktree_uses_advanced_tranche_integration_head(self) -> None:
        subprocess.run(("git", "checkout", "--", "app.py"), cwd=self.repo, check=True)
        self.ledger.connection.execute("INSERT INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F','f','f','planned',1,1)")
        self.ledger.connection.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?,'[]')", (self.base,))
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (self.ticket_id,))

        advanced_worktree = self.root / "advanced"
        subprocess.run(("git", "worktree", "add", "-q", "-b", "advanced-test", str(advanced_worktree), self.base), cwd=self.repo, check=True)
        (advanced_worktree / "app.py").write_text('def value():\n    return "advanced"\n')
        subprocess.run(("git", "add", "app.py"), cwd=advanced_worktree, check=True)
        subprocess.run(("git", "commit", "-qm", "advanced"), cwd=advanced_worktree, check=True)
        advanced = subprocess.run(("git", "rev-parse", "HEAD"), cwd=advanced_worktree, text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(("git", "worktree", "remove", "--force", str(advanced_worktree)), cwd=self.repo, check=True)
        subprocess.run(("git", "update-ref", "refs/local-first/tranches/T/integration-head", advanced), cwd=self.repo, check=True)

        result = self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")
        target = self.repo / ".worktrees" / "H-1"

        self.assertEqual(result["workspace_path"], str(target.resolve()))
        self.assertEqual(result["branch_name"], "wt/H-1")
        self.assertEqual(result["base_sha"], advanced)
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD"), cwd=target, text=True, capture_output=True, check=True).stdout.strip(), advanced)
        self.assertEqual(subprocess.run(("git", "branch", "--show-current"), cwd=target, text=True, capture_output=True, check=True).stdout.strip(), "wt/H-1")
        self.assertEqual(subprocess.run(("git", "rev-parse", "HEAD"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip(), self.base)
        self.assertEqual(self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1"), result)

        (target / "app.py").write_text('def value():\n    return "dirty"\n')
        with self.assertRaisesRegex(RuntimeError, "existing worktree drift"):
            self.controller.prepare_hermes_dispatch_worktree(self.ticket_id, "H-1")

    def test_scheduler_auto_reconciles_blocked_handoff_then_validation_wins_next_tick(self) -> None:
        self._make_generated_owned()
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "blocked", str(self.repo)),
            session_id="session-7",
            branch_name="worker-branch",
            started_at=10,
            completed_at=20,
            runs=(ExternalExecutionRun(7, "blocked", "blocked", 10, 20, HANDOFF_SENTINEL, "worker-code", 123, {"source": "dispatcher"}),),
        )
        implementation_calls: list[str] = []

        def reconcile_one():
            for candidate in self.ledger.hermes_execution_candidates():
                try:
                    return self.controller.reconcile_hermes_execution(str(candidate["external_task_id"]), require_handoff=True)
                except RuntimeError as exc:
                    if str(exc) == "no unreconciled Hermes worker run available for reconciliation":
                        continue
                    raise
            return None

        scheduler = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="scheduler",
            lease_seconds=30,
            clock=lambda: 100,
            hermes_execution_runner=reconcile_one,
            implementation_runner=lambda ticket_id: implementation_calls.append(ticket_id) or (_ for _ in ()).throw(AssertionError("local implementation must not run")),
            validation_runner=lambda ticket_id: self.controller.execute_deterministic_validation_only(ticket_id, repository=self.repo),
        )
        first = scheduler.process_next()
        self.assertEqual((first.status, first.stage, first.ticket_id), ("reconciled_external", "implementation", self.ticket_id))
        self.assertEqual(implementation_calls, [])
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 0)

        second = scheduler.process_next()
        self.assertEqual((second.status, second.stage, second.ticket_id), ("completed", "validation", self.ticket_id))
        self.assertEqual(implementation_calls, [])
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 1)

    def test_second_blocked_handoff_becomes_second_attempt_for_hermes_owned_repair(self) -> None:
        self._make_generated_owned()
        first_run = ExternalExecutionRun(7, "blocked", "blocked", 10, 20, HANDOFF_SENTINEL, "worker-code", 123, {"source": "dispatcher"})
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "blocked", str(self.repo)),
            session_id="session-7",
            branch_name="worker-branch",
            started_at=10,
            completed_at=20,
            runs=(first_run,),
        )
        first = self.controller.reconcile_hermes_execution("H-1", require_handoff=True)
        self.assertEqual(first["attempt_number"], 1)
        with self.assertRaisesRegex(RuntimeError, "no unreconciled Hermes worker run"):
            self.controller.reconcile_hermes_execution("H-1", require_handoff=True)

        self.ledger.connection.execute("UPDATE tickets SET state=? WHERE id=?", (CanonicalState.REPAIRING.value, self.ticket_id))
        (self.repo / "app.py").write_text('def value():\n    return "repaired"\n')
        second_run = ExternalExecutionRun(8, "blocked", "blocked", 21, 30, HANDOFF_SENTINEL, "worker-code", 456, {"source": "dispatcher"})
        self.board.snapshot = ExternalExecutionSnapshot(
            task=ExternalTicket("H-1", "external", "", "blocked", str(self.repo)),
            session_id="session-8",
            branch_name="worker-branch",
            started_at=21,
            completed_at=30,
            runs=(first_run, second_run),
        )
        second = self.controller.reconcile_hermes_execution("H-1", require_handoff=True)
        self.assertEqual(second["attempt_number"], 2)
        self.assertEqual(second["hermes_run_id"], 8)
        self.assertEqual(self.ledger.attempt_count(self.ticket_id), 2)
        self.assertEqual(self.ledger.get_ticket(self.ticket_id)["state"], CanonicalState.IMPLEMENTING.value)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM hermes_execution_reconciliations WHERE ticket_id=?", (self.ticket_id,)).fetchone()[0], 2)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM model_invocations WHERE ticket_id=? AND stage='implementation'", (self.ticket_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
