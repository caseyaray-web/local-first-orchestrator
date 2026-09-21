from __future__ import annotations

import hashlib
import base64
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig, _isolated_candidate_diff
from local_first_orchestrator.cli import main as cli_main
from local_first_orchestrator.execution_handoff import HANDOFF_MARKER, HANDOFF_SENTINEL
from local_first_orchestrator.hermes_board import ExternalExecutionRun, ExternalExecutionSnapshot, ExternalTicket, HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger, _stable_scheduler_failure_fingerprint
from local_first_orchestrator.native_release_approval import canonical_validation_recovery_bytes, fingerprint_public_key
from local_first_orchestrator.operator_config import ModelRegistration, OperatorConfig, load_operator_config, save_operator_config
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState
from local_first_orchestrator.triage import LocalTriagePlanner
from local_first_orchestrator.validation import DeterministicValidator
import local_first_orchestrator.validation_recovery_boundary as recovery_boundary


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
        self.signing_key = Ed25519PrivateKey.generate()
        self.signer_public_key = self.signing_key.public_key().public_bytes_raw()
        self.signer_fingerprint = fingerprint_public_key(self.signer_public_key)
        self.signer_authority_hash = hashlib.sha256(self.signer_fingerprint.encode("ascii")).hexdigest()
        self.operator_config_path = self.root / "operator-config.json"
        self.operator_config = OperatorConfig(
            ledger_path=self.ledger.database,
            canonical_repository=self.repo,
            repository_allowlist=(self.repo,),
            implementation=ModelRegistration("implementation", "fixture", "fixture"),
            review=ModelRegistration("review", "fixture", "fixture"),
            worktree_root=self.root / "worktrees",
            artifact_root=self.root / "artifacts",
            implementation_timeout_seconds=300,
            review_timeout_seconds=300,
            operator_signing_public_key=base64.b64encode(self.signer_public_key).decode("ascii"),
            operator_signing_key_fingerprint=self.signer_fingerprint,
        )
        save_operator_config(self.operator_config, self.operator_config_path)
        self.recovery_approvals: dict[tuple[int, str, str], tuple[dict[str, object], bytes]] = {}
        self.ledger.bind_runtime(self.ticket_id, str(self.repo), self.base, operator_signer_fingerprint=self.signer_fingerprint, operator_authority_hash=self.signer_authority_hash)
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
            load_operator_config(self.operator_config_path).runtime_config(),
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def _signed_recovery_material(self, attempt_number: int, reason: str, *, operator_id: str = "operator") -> tuple[dict[str, object], bytes]:
        key = (attempt_number, operator_id, reason)
        existing = self.recovery_approvals.get(key)
        if existing is not None:
            return existing
        prepared = self.controller.prepare_validation_controller_defect_recovery(
            self.ticket_id,
            attempt_number,
            operator_id=operator_id,
            reason=reason,
        )
        document = prepared["document"]
        assert isinstance(document, dict)
        signature = self.signing_key.sign(canonical_validation_recovery_bytes(document))
        material = (document, signature)
        self.recovery_approvals[key] = material
        return material

    def _signed_recover(self, attempt_number: int, reason: str, *, operator_id: str = "operator") -> dict[str, object]:
        document, signature = self._signed_recovery_material(attempt_number, reason, operator_id=operator_id)
        return self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id,
            attempt_number,
            repository=self.repo,
            operator_id=operator_id,
            reason=reason,
            approval_document=document,
            detached_signature=signature,
        )

    def _config_for_signing_key(self, signing_key: Ed25519PrivateKey) -> tuple[OperatorConfig, bytes, str, str]:
        public_key = signing_key.public_key().public_bytes_raw()
        fingerprint = fingerprint_public_key(public_key)
        authority_hash = hashlib.sha256(fingerprint.encode("ascii")).hexdigest()
        config = OperatorConfig(
            ledger_path=self.ledger.database,
            canonical_repository=self.repo,
            repository_allowlist=(self.repo,),
            implementation=ModelRegistration("implementation", "fixture", "fixture"),
            review=ModelRegistration("review", "fixture", "fixture"),
            worktree_root=self.root / "worktrees",
            artifact_root=self.root / "artifacts",
            implementation_timeout_seconds=300,
            review_timeout_seconds=300,
            operator_signing_public_key=base64.b64encode(public_key).decode("ascii"),
            operator_signing_key_fingerprint=fingerprint,
        )
        return config, public_key, fingerprint, authority_hash

    def _production_triage_board(self) -> HermesBoardAdapter:
        remote = {"status": "blocked"}
        def runner(argv, **kwargs):
            args = tuple(argv)
            command = args[4] if len(args) > 4 else ""
            if command == "show":
                payload = {
                    "task": {
                        "id": "H-1", "title": "external", "body": "", "status": remote["status"],
                        "workspace_path": str(self.repo), "assignee": None, "workspace_kind": "worktree",
                        "repository_identity": str(self.repo), "base_sha": self.base,
                        "session_id": None, "branch_name": None, "started_at": None, "completed_at": None,
                    },
                    "latest_summary": None, "parents": [], "children": [], "comments": [], "events": [], "runs": [],
                }
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
            if command == "schedule":
                remote["status"] = "scheduled"
                return subprocess.CompletedProcess(args, 0, stdout="scheduled", stderr="")
            if command == "block":
                remote["status"] = "blocked"
                return subprocess.CompletedProcess(args, 0, stdout="blocked", stderr="")
            if command in {"comment", "complete", "unblock"}:
                if command == "complete": remote["status"] = "done"
                if command == "unblock": remote["status"] = "ready"
                return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
            raise AssertionError(args)
        return HermesBoardAdapter(
            board="test", executable="/bin/true", runner=runner, allow_writes=True,
            timeout_seconds=2, canonical_repository=self.repo,
        )

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

        recovered = self._signed_recover(1, "validator false positive corrected")

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
        replayed = self._signed_recover(1, "validator false positive corrected")
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
        recovered = self._signed_recover(1, "validator false positive corrected")
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
        triage_policy_hash = None
        if with_triage_claim:
            payload = {
                "classification": "architecture_gap",
                "root_cause_evidence": "The obsolete validator masked a controller-level validation defect.",
                "recommended_action": "block",
                "children": [],
            }
            def triage_runner(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
            planner = LocalTriagePlanner(
                runner=triage_runner,
                provider="matrix-triage-provider",
                model="matrix-triage-model",
                profile="matrix-triage-profile",
                timeout_seconds=23,
            )
            triage_policy_hash = planner.execution_policy_hash()
            scheduler = ProcessNextScheduler(
                self.ledger,
                self._production_triage_board(),
                worker_id="matrix-triage-worker",
                target_ticket_id=self.ticket_id,
                lease_seconds=60,
                clock=lambda: 112,
                triage_runner=lambda ticket_id: self.controller.execute_triage_only(ticket_id, planner=planner),
                triage_execution_policy_hash=triage_policy_hash,
            )
            triaged = None
            for _ in range(6):
                result = scheduler.process_next()
                if result.stage == "triage":
                    triaged = result
                    break
            self.assertIsNotNone(triaged)
            assert triaged is not None
            self.assertEqual(triaged.status, "completed")
            self.assertIsNotNone(self.ledger.model_stage(self.ticket_id, 1, "triage"))
            self.assertIsNotNone(self.ledger.runtime_stage(self.ticket_id, "triage-applied-1"))
            completed_triage = self.ledger.connection.execute(
                "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage='triage:1' AND status='completed'",
                (self.ticket_id,),
            ).fetchone()
            self.assertIsNotNone(completed_triage)
        self.ledger.record_terminal_unresolvable(
            self.ticket_id,
            attempt_number=1,
            failure_fingerprint=str(proposed["failure_fingerprint"]),
            reason="attempt limit reached after deterministic validation failure",
            summary={"deterministic_failure": {"source": "validation", "attempt_number": 1, "failure_evidence": false_result["compact_evidence"]}},
            notification_target="mattermost:ops",
        )
        self.ledger.pause("operator", reason="matrix recovery")
        self._signed_recovery_material(1, "matrix validator correction")
        return {
            "routing_result": routing_result,
            "routing_claim_id": routing_claim_id,
            "triage_policy_hash": triage_policy_hash,
            "validation_claim_result": validation_claim_result,
        }

    def _recovery_state_snapshot(self) -> dict[str, object]:
        tables = {
            "ticket": "SELECT * FROM tickets WHERE id=?",
            "claims": "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? ORDER BY claim_id",
            "stages": "SELECT * FROM runtime_stages WHERE ticket_id=? ORDER BY stage",
            "archives": "SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id",
            "authorities": "SELECT * FROM validation_recovery_authorities WHERE ticket_id=? ORDER BY authority_id",
            "model_stages": "SELECT * FROM model_stage_artifacts WHERE ticket_id=? ORDER BY attempt_number,stage",
            "notifications": "SELECT * FROM gateway_notification_outbox WHERE ticket_id=? ORDER BY operation_id",
            "events": "SELECT * FROM events WHERE entity_type='ticket' AND entity_id=? ORDER BY id",
            "terminal": "SELECT * FROM terminal_ticket_failures WHERE ticket_id=?",
        }
        snapshot: dict[str, object] = {}
        for name, query in tables.items():
            rows = self.ledger.connection.execute(query, (self.ticket_id,)).fetchall()
            snapshot[name] = [tuple(row) for row in rows]
        return snapshot

    def _attempt_matrix_recovery(self) -> dict[str, object]:
        return self._signed_recover(1, "matrix validator correction")

    def test_recovery_rejects_runtime_route_attempt_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        self.ledger.connection.execute("UPDATE runtime_stages SET attempt_number=2 WHERE ticket_id=? AND stage='repair-routing-1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "routing lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_triage_canonical_identity_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=? WHERE ticket_id=? AND stage='triage:1'", (json.dumps({"ticket_id": self.ticket_id, "attempt_number": 1, "parent_depth": 999, "unresolved_criteria": [], "failure_evidence_hash": "x", "ticket_policy_hash": "x", "triage_execution_policy_hash": "triage-policy-for-matrix"}, sort_keys=True, separators=(",", ":")), self.ticket_id))
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "triage lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_recovery_rejects_triage_failure_evidence_hash_mismatch_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        claim = self.ledger.connection.execute("SELECT candidate_identity_json FROM scheduler_stage_claims WHERE ticket_id=? AND stage='triage:1'", (self.ticket_id,)).fetchone()
        identity = json.loads(str(claim["candidate_identity_json"]))
        identity["failure_evidence_hash"] = "0" * 64
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=?,result_json=? WHERE ticket_id=? AND stage='triage:1'", (encoded, json.dumps({"candidate_identity": identity}, sort_keys=True, separators=(",", ":")), self.ticket_id))
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
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET lease_owner='leased',lease_expires_at=999 WHERE ticket_id=? AND stage='triage:1'", (self.ticket_id,))
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
        self.assertIsNone(self.ledger.runtime_stage(self.ticket_id, "triage-applied-1"))
        archived = self.ledger.connection.execute("SELECT source_claim_result_json,source_claim_snapshot_json FROM validation_recovery_archives WHERE ticket_id=? AND archive_kind='triage_claim'", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(archived)
        assert archived is not None
        result_identity = json.loads(str(archived["source_claim_result_json"]))["candidate_identity"]
        snapshot_identity = json.loads(str(json.loads(str(archived["source_claim_snapshot_json"]))["candidate_identity_json"]))
        self.assertEqual(result_identity, snapshot_identity)

        applied_archive = self.ledger.connection.execute(
            "SELECT source_runtime_detail,source_runtime_stage FROM validation_recovery_archives WHERE ticket_id=? AND archive_kind='triage_claim'",
            (self.ticket_id,),
        ).fetchone()
        self.assertIsNotNone(applied_archive)
        self.assertEqual(applied_archive["source_runtime_stage"], "triage-applied-1")
        self.assertEqual(json.loads(str(applied_archive["source_runtime_detail"])), json.loads(str(archived["source_claim_result_json"])))
        model_stage = self.ledger.model_stage(self.ticket_id, 1, "triage")
        self.assertIsNotNone(model_stage)
        self.assertTrue(Path(str(model_stage["response_artifact"])).is_file())
        triage_result = json.loads(str(archived["source_claim_result_json"]))
        artifact_envelope = json.loads(Path(str(model_stage["response_artifact"])).read_text(encoding="utf-8"))
        expected_proposal_hash = hashlib.sha256(json.dumps(artifact_envelope["payload"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(triage_result["proposal_hash"], expected_proposal_hash)

    def _prepare_archive_only_replay_state(self) -> None:
        prepared = self._prepare_stale_validation_recovery(with_triage_claim=True)
        self._attempt_matrix_recovery()
        self.ledger.connection.execute(
            "UPDATE tickets SET state=? WHERE id=?",
            (CanonicalState.VERIFYING.value, self.ticket_id),
        )
        self.ledger.connection.execute(
            "UPDATE scheduler_stage_claims SET result_json=? WHERE ticket_id=? AND stage='validation:1'",
            (json.dumps(prepared["validation_claim_result"], sort_keys=True, separators=(",", ":")), self.ticket_id),
        )
        self.ledger.connection.execute(
            "DELETE FROM runtime_stages WHERE ticket_id=? AND stage IN ('validation-1','validation_completed')",
            (self.ticket_id,),
        )

    def _prepare_marker_reconciliation_state(self):
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self._attempt_matrix_recovery()
        self.ledger.connection.execute(
            "UPDATE runtime_stages SET attempt_number=99 WHERE ticket_id=? AND stage='validation_completed'",
            (self.ticket_id,),
        )
        authority = self.ledger.connection.execute(
            "SELECT * FROM validation_recovery_authorities WHERE ticket_id=? AND attempt_number=1",
            (self.ticket_id,),
        ).fetchone()
        self.assertIsNotNone(authority)
        return authority


    def test_marker_reconciliation_external_authority_matrix_is_fail_closed(self) -> None:
        genuine = self._prepare_marker_reconciliation_state()
        authority_columns = [desc[0] for desc in self.ledger.connection.execute("SELECT * FROM validation_recovery_authorities LIMIT 0").description]
        genuine_values = [genuine[column] for column in authority_columns]
        genuine_document = json.loads(str(genuine["document_json"]))
        genuine_binding = self.ledger.runtime_binding(self.ticket_id)
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_authorities_immutable_update")
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_authorities_immutable_delete")
        self.ledger.connection.execute("DROP TRIGGER runtime_bindings_signer_immutable")

        def restore_authority() -> None:
            self.ledger.connection.execute("DELETE FROM validation_recovery_authorities WHERE ticket_id=?", (self.ticket_id,))
            self.ledger.connection.execute(
                f"INSERT INTO validation_recovery_authorities({','.join(authority_columns)}) VALUES ({','.join('?' for _ in authority_columns)})",
                genuine_values,
            )
            self.ledger.connection.execute(
                "UPDATE runtime_bindings SET operator_signer_fingerprint=?,operator_authority_hash=? WHERE ticket_id=?",
                (genuine_binding["operator_signer_fingerprint"], genuine_binding["operator_authority_hash"], self.ticket_id),
            )
            save_operator_config(self.operator_config, self.operator_config_path)

        def assert_rejected_without_mutation() -> None:
            before = self.ledger.connection.serialize()
            with self.assertRaises((PermissionError, ValueError)):
                self.controller.reconcile_validation_controller_defect_completion_marker(
                    self.ticket_id, 1, operator_id="operator", reason="late marker repair"
                )
            self.assertEqual(before, self.ledger.connection.serialize())

        attacker_key = Ed25519PrivateKey.generate()
        _, _, attacker_fp, attacker_hash = self._config_for_signing_key(attacker_key)
        forged = json.loads(json.dumps(genuine_document))
        forged["authority"]["signer_fingerprint"] = attacker_fp
        forged["authority"]["runtime_authority_hash"] = attacker_hash
        forged_raw = canonical_validation_recovery_bytes(forged)
        self.ledger.connection.execute(
            "UPDATE runtime_bindings SET operator_signer_fingerprint=?,operator_authority_hash=? WHERE ticket_id=?",
            (attacker_fp, attacker_hash, self.ticket_id),
        )
        self.ledger.connection.execute(
            "UPDATE validation_recovery_authorities SET signer_fingerprint=?,document_json=?,document_hash=?,detached_signature=? WHERE ticket_id=?",
            (attacker_fp, forged_raw.decode("utf-8"), hashlib.sha256(forged_raw).hexdigest(), attacker_key.sign(forged_raw), self.ticket_id),
        )
        assert_rejected_without_mutation()
        restore_authority()

        cases = []
        cases.append(("canonical_document", lambda: self.ledger.connection.execute("UPDATE validation_recovery_authorities SET document_json=' ' || document_json WHERE ticket_id=?", (self.ticket_id,))))
        cases.append(("document_hash", lambda: self.ledger.connection.execute("UPDATE validation_recovery_authorities SET document_hash=? WHERE ticket_id=?", ("0" * 64, self.ticket_id))))
        cases.append(("row_signer_fingerprint", lambda: self.ledger.connection.execute("UPDATE validation_recovery_authorities SET signer_fingerprint=? WHERE ticket_id=?", (attacker_fp, self.ticket_id))))
        for label, mutate in cases:
            with self.subTest(case=label):
                mutate(); assert_rejected_without_mutation(); restore_authority()

        for label, field, value in (
            ("runtime_authority_hash", "runtime_authority_hash", attacker_hash),
            ("ticket", "ticket_id", "forged-ticket"),
            ("attempt", "attempt_number", 2),
            ("generation", "terminal_generation", 999),
        ):
            with self.subTest(case=label):
                modified = json.loads(json.dumps(genuine_document)); modified["authority"][field] = value
                raw = canonical_validation_recovery_bytes(modified)
                self.ledger.connection.execute(
                    "UPDATE validation_recovery_authorities SET document_json=?,document_hash=?,detached_signature=? WHERE ticket_id=?",
                    (raw.decode("utf-8"), hashlib.sha256(raw).hexdigest(), self.signing_key.sign(raw), self.ticket_id),
                )
                assert_rejected_without_mutation(); restore_authority()

        with self.subTest(case="archive_identity"):
            modified = json.loads(json.dumps(genuine_document)); modified["authority"]["expected_archive_ids"] = ["forged-archive"]
            raw = canonical_validation_recovery_bytes(modified)
            self.ledger.connection.execute(
                "UPDATE validation_recovery_authorities SET document_json=?,document_hash=?,detached_signature=?,expected_archive_ids_json=? WHERE ticket_id=?",
                (raw.decode("utf-8"), hashlib.sha256(raw).hexdigest(), self.signing_key.sign(raw), json.dumps(["forged-archive"]), self.ticket_id),
            )
            assert_rejected_without_mutation(); restore_authority()

        with self.subTest(case="external_config_drift"):
            attacker_config, _, _, _ = self._config_for_signing_key(attacker_key)
            save_operator_config(attacker_config, self.operator_config_path)
            assert_rejected_without_mutation(); restore_authority()

    def test_marker_reconciliation_valid_configured_authority_succeeds_through_controller(self) -> None:
        self._prepare_marker_reconciliation_state()
        result = self.controller.reconcile_validation_controller_defect_completion_marker(
            self.ticket_id, 1, operator_id="operator", reason="matrix validator correction"
        )
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(self.ledger.runtime_stage(self.ticket_id, "validation_completed")["attempt_number"], 1)

    def test_marker_reconciliation_valid_configured_authority_succeeds_through_cli(self) -> None:
        self._prepare_marker_reconciliation_state()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--database", str(self.ledger.database),
                "--operator-config-path", str(self.operator_config_path),
                "--hermes-executable", "/bin/true",
                "--board", "isolated",
                "reconcile-validation-controller-defect-marker",
                "--task-id", self.ticket_id,
                "--attempt-number", "1",
                "--operator-id", "operator",
                "--reason", "matrix validator correction",
            ]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "reconciled")
        self.assertEqual(self.ledger.runtime_stage(self.ticket_id, "validation_completed")["attempt_number"], 1)

    def test_archive_only_replay_rejects_partial_archive_set_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_delete")
        self.ledger.connection.execute(
            "DELETE FROM validation_recovery_archives WHERE ticket_id=? AND archive_kind='triage_claim'",
            (self.ticket_id,),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "archive set|triage lineage"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_mismatched_archived_source_identity_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_update")
        self.ledger.connection.execute(
            "UPDATE validation_recovery_archives SET source_claim_id='forged-claim' WHERE ticket_id=? AND archive_kind='triage_claim'",
            (self.ticket_id,),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "archive triage source identity|archive source identity"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_wrong_generation_archive_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_update")
        self.ledger.connection.execute(
            "UPDATE validation_recovery_archives SET terminal_generation=terminal_generation+1 WHERE ticket_id=? AND archive_kind='repair_routing_stage'",
            (self.ticket_id,),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "archive set|archive source identity"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_unrelated_archive_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute(
            "INSERT INTO validation_recovery_archives(archive_id,ticket_id,attempt_number,terminal_generation,archive_kind,operator_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"forged-extra:{self.ticket_id}:1:g1:triage_feedback_stage", self.ticket_id, 1, 1, "triage_feedback_stage", "operator", "matrix validator correction", 999),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaisesRegex(ValueError, "archive set|unrelated evidence"):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_forged_event_and_archives_without_signed_authority(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_authorities_immutable_delete")
        self.ledger.connection.execute("DELETE FROM validation_recovery_authorities WHERE ticket_id=?", (self.ticket_id,))
        archive_ids = [str(row[0]) for row in self.ledger.connection.execute("SELECT archive_id FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_kind", (self.ticket_id,)).fetchall()]
        self.ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,actor_type,actor_id,payload_json,created_at) VALUES ('ticket',?,?, 'controller',?,?,?)",
            (self.ticket_id, "validation_controller_defect_recovery_authorized", "operator", json.dumps({"attempt_number": 1, "terminal_generation": 1, "archived_downstream_identities": archive_ids, "reason": "matrix validator correction"}, sort_keys=True, separators=(",", ":")), 999),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaises((PermissionError, ValueError)):
            self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_directly_forged_authority_row_with_fake_signature(self) -> None:
        self._prepare_archive_only_replay_state()
        genuine = self.ledger.connection.execute("SELECT * FROM validation_recovery_authorities WHERE ticket_id=?", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(genuine)
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_authorities_immutable_delete")
        self.ledger.connection.execute("DELETE FROM validation_recovery_authorities WHERE ticket_id=?", (self.ticket_id,))
        self.ledger.connection.execute(
            "INSERT INTO validation_recovery_authorities(authority_id,ticket_id,attempt_number,terminal_generation,operator_id,reason,signer_fingerprint,document_json,document_hash,detached_signature,expected_archive_ids_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (genuine["authority_id"], genuine["ticket_id"], genuine["attempt_number"], genuine["terminal_generation"], genuine["operator_id"], genuine["reason"], genuine["signer_fingerprint"], genuine["document_json"], genuine["document_hash"], b"x" * 64, genuine["expected_archive_ids_json"], 999),
        )
        before = self._recovery_state_snapshot()
        with self.assertRaises(ValueError): self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_wrong_typed_archive_identity_columns_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_update")
        for column in ("attempt_number", "terminal_generation"):
            with self.subTest(column=column):
                self.ledger.connection.execute(f"UPDATE validation_recovery_archives SET {column}=CAST('1' AS BLOB) WHERE ticket_id=? AND archive_kind='repair_routing_stage'", (self.ticket_id,))
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError): self._attempt_matrix_recovery()
                self.assertEqual(before, self._recovery_state_snapshot())
                self.ledger.connection.execute(f"UPDATE validation_recovery_archives SET {column}=1 WHERE ticket_id=? AND archive_kind='repair_routing_stage'", (self.ticket_id,))

    def test_recovery_rejects_boolean_and_string_routing_attempt_aliases_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        stage = self.ledger.runtime_stage(self.ticket_id, "repair-routing-1")
        claim = self.ledger.connection.execute("SELECT claim_id,result_json FROM scheduler_stage_claims WHERE ticket_id=? AND stage='repair_routing:1'", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(stage); self.assertIsNotNone(claim)
        original_detail = str(stage["detail"]); original_result = str(claim["result_json"])
        for target, alias in (("detail", True), ("detail", "1"), ("result", True), ("result", "1")):
            with self.subTest(target=target, alias=repr(alias)):
                detail = json.loads(original_detail); result = json.loads(original_result)
                if target == "detail": detail["attempt_number"] = alias
                else: result["attempt_number"] = alias
                self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='repair-routing-1'", (json.dumps(detail, sort_keys=True, separators=(",", ":")), self.ticket_id))
                self.ledger.connection.execute("UPDATE scheduler_stage_claims SET result_json=? WHERE claim_id=?", (json.dumps(result, sort_keys=True, separators=(",", ":")), claim["claim_id"]))
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError): self._attempt_matrix_recovery()
                self.assertEqual(before, self._recovery_state_snapshot())
                self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='repair-routing-1'", (original_detail, self.ticket_id))
                self.ledger.connection.execute("UPDATE scheduler_stage_claims SET result_json=? WHERE claim_id=?", (original_result, claim["claim_id"]))

    def test_recovery_rejects_boolean_and_string_triage_attempt_aliases_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        claim = self.ledger.connection.execute("SELECT claim_id,candidate_identity_json,result_json FROM scheduler_stage_claims WHERE ticket_id=? AND stage='triage:1'", (self.ticket_id,)).fetchone()
        applied = self.ledger.runtime_stage(self.ticket_id, "triage-applied-1")
        self.assertIsNotNone(claim); self.assertIsNotNone(applied)
        original_identity = str(claim["candidate_identity_json"]); original_result = str(claim["result_json"]); original_applied = str(applied["detail"])
        for target, alias in (("identity", True), ("identity", "1"), ("result", True), ("result", "1")):
            with self.subTest(target=target, alias=repr(alias)):
                identity = json.loads(original_identity); result = json.loads(original_result); applied_detail = json.loads(original_applied)
                if target == "identity":
                    identity["attempt_number"] = alias
                    result["candidate_identity"]["attempt_number"] = alias
                    applied_detail["candidate_identity"]["attempt_number"] = alias
                else:
                    result["attempt_number"] = alias
                    applied_detail["attempt_number"] = alias
                self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=?,result_json=? WHERE claim_id=?", (json.dumps(identity, sort_keys=True, separators=(",", ":")), json.dumps(result, sort_keys=True, separators=(",", ":")), claim["claim_id"]))
                self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='triage-applied-1'", (json.dumps(applied_detail, sort_keys=True, separators=(",", ":")), self.ticket_id))
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError): self._attempt_matrix_recovery()
                self.assertEqual(before, self._recovery_state_snapshot())
                self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json=?,result_json=? WHERE claim_id=?", (original_identity, original_result, claim["claim_id"]))
                self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='triage-applied-1'", (original_applied, self.ticket_id))

    def test_signed_authority_rejects_boolean_and_string_attempt_generation_aliases_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        original, _ = self._signed_recovery_material(1, "matrix validator correction")
        for field, alias in (("attempt_number", True), ("attempt_number", "1"), ("terminal_generation", True), ("terminal_generation", "1")):
            with self.subTest(field=field, alias=repr(alias)):
                malformed = json.loads(json.dumps(original))
                malformed["authority"][field] = alias
                signature = self.signing_key.sign(canonical_validation_recovery_bytes(malformed))
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError):
                    self.controller.recover_terminal_validation_controller_defect(self.ticket_id, 1, repository=self.repo, operator_id="operator", reason="matrix validator correction", approval_document=malformed, detached_signature=signature)
                self.assertEqual(before, self._recovery_state_snapshot())

    def test_primary_recovery_rejects_attacker_runtime_binding_and_key_against_external_config_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        attacker_key = Ed25519PrivateKey.generate()
        _, _, attacker_fingerprint, attacker_authority_hash = self._config_for_signing_key(attacker_key)
        self.ledger.connection.execute("DROP TRIGGER runtime_bindings_signer_immutable")
        self.ledger.connection.execute(
            "UPDATE runtime_bindings SET operator_signer_fingerprint=?,operator_authority_hash=? WHERE ticket_id=?",
            (attacker_fingerprint, attacker_authority_hash, self.ticket_id),
        )
        prepared = self.controller.prepare_validation_controller_defect_recovery(
            self.ticket_id, 1, operator_id="operator", reason="attacker-forged authority"
        )
        document = prepared["document"]
        assert isinstance(document, dict)
        signature = attacker_key.sign(canonical_validation_recovery_bytes(document))
        before = self.ledger.connection.serialize()
        with self.assertRaises(PermissionError):
            self.controller.recover_terminal_validation_controller_defect(
                self.ticket_id,
                1,
                repository=self.repo,
                operator_id="operator",
                reason="attacker-forged authority",
                approval_document=document,
                detached_signature=signature,
            )
        self.assertEqual(before, self.ledger.connection.serialize())

    def test_primary_recovery_rejects_external_config_key_drift_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery()
        document, signature = self._signed_recovery_material(1, "matrix validator correction")
        attacker_key = Ed25519PrivateKey.generate()
        attacker_config, _, _, _ = self._config_for_signing_key(attacker_key)
        save_operator_config(attacker_config, self.operator_config_path)
        before = self.ledger.connection.serialize()
        try:
            with self.assertRaises(PermissionError):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id,
                    1,
                    repository=self.repo,
                    operator_id="operator",
                    reason="matrix validator correction",
                    approval_document=document,
                    detached_signature=signature,
                )
            self.assertEqual(before, self.ledger.connection.serialize())
        finally:
            save_operator_config(self.operator_config, self.operator_config_path)

    def test_completed_triage_recovery_rejects_missing_or_bad_model_stage_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        row = self.ledger.model_stage(self.ticket_id, 1, "triage"); self.assertIsNotNone(row)
        original_hash = str(row["diff_hash"])
        self.ledger.connection.execute("DELETE FROM model_stage_artifacts WHERE ticket_id=? AND attempt_number=1 AND stage='triage'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaises(ValueError): self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())
        # Recreate from the genuine row, then prove identity-hash corruption also fails.
        columns = [desc[0] for desc in self.ledger.connection.execute("SELECT * FROM model_stage_artifacts LIMIT 0").description]
        values = [row[column] for column in columns]
        self.ledger.connection.execute(f"INSERT INTO model_stage_artifacts({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET diff_hash=? WHERE ticket_id=? AND attempt_number=1 AND stage='triage'", ("0" * 64, self.ticket_id))
        before = self._recovery_state_snapshot()
        with self.assertRaises(ValueError): self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())
        self.ledger.connection.execute("UPDATE model_stage_artifacts SET diff_hash=? WHERE ticket_id=? AND attempt_number=1 AND stage='triage'", (original_hash, self.ticket_id))

    def test_completed_triage_recovery_rejects_missing_replaced_malformed_or_tampered_artifact_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        row = self.ledger.model_stage(self.ticket_id, 1, "triage"); self.assertIsNotNone(row)
        path = Path(str(row["response_artifact"])); original = path.read_bytes()
        mutations = (None, b"not-json", json.dumps({"provider":"other","model":"other","payload":{"recommended_action":"block"}}, sort_keys=True).encode(), original.replace(b"architecture_gap", b"implementation_gap"))
        for payload in mutations:
            with self.subTest(payload="missing" if payload is None else hashlib.sha256(payload).hexdigest()[:8]):
                if path.exists(): path.unlink()
                if payload is not None: path.write_bytes(payload)
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError): self._attempt_matrix_recovery()
                self.assertEqual(before, self._recovery_state_snapshot())
                path.write_bytes(original)

    def test_completed_triage_recovery_rejects_missing_or_altered_applied_stage_without_mutation(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        applied = self.ledger.runtime_stage(self.ticket_id, "triage-applied-1"); self.assertIsNotNone(applied)
        original = str(applied["detail"])
        self.ledger.connection.execute("DELETE FROM runtime_stages WHERE ticket_id=? AND stage='triage-applied-1'", (self.ticket_id,))
        before = self._recovery_state_snapshot()
        with self.assertRaises(ValueError): self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())
        self.ledger.connection.execute("INSERT INTO runtime_stages(ticket_id,stage,detail,attempt_number,artifact_path,artifact_sha256,base_sha,created_at) VALUES (?,?,?,?,?,?,?,?)", (self.ticket_id,"triage-applied-1",original,1,applied["artifact_path"],applied["artifact_sha256"],applied["base_sha"],applied["created_at"]))
        detail = json.loads(original); detail["proposal_hash"] = "0" * 64
        self.ledger.connection.execute("UPDATE runtime_stages SET detail=? WHERE ticket_id=? AND stage='triage-applied-1'", (json.dumps(detail, sort_keys=True, separators=(",", ":")), self.ticket_id))
        before = self._recovery_state_snapshot()
        with self.assertRaises(ValueError): self._attempt_matrix_recovery()
        self.assertEqual(before, self._recovery_state_snapshot())

    def test_archive_only_replay_rejects_archived_triage_result_applied_or_proposal_mismatch_without_mutation(self) -> None:
        self._prepare_archive_only_replay_state()
        self.ledger.connection.execute("DROP TRIGGER validation_recovery_archives_immutable_update")
        original = self.ledger.connection.execute("SELECT source_claim_result_json,source_runtime_detail FROM validation_recovery_archives WHERE ticket_id=? AND archive_kind='triage_claim'", (self.ticket_id,)).fetchone()
        self.assertIsNotNone(original)
        for field in ("result", "applied"):
            with self.subTest(field=field):
                result = json.loads(str(original["source_claim_result_json"])); applied = json.loads(str(original["source_runtime_detail"]))
                if field == "result": result["proposal_hash"] = "0" * 64
                else: applied["proposal_hash"] = "0" * 64
                self.ledger.connection.execute("UPDATE validation_recovery_archives SET source_claim_result_json=?,source_runtime_detail=? WHERE ticket_id=? AND archive_kind='triage_claim'", (json.dumps(result, sort_keys=True, separators=(",", ":")), json.dumps(applied, sort_keys=True, separators=(",", ":")), self.ticket_id))
                before = self._recovery_state_snapshot()
                with self.assertRaises(ValueError): self._attempt_matrix_recovery()
                self.assertEqual(before, self._recovery_state_snapshot())
                self.ledger.connection.execute("UPDATE validation_recovery_archives SET source_claim_result_json=?,source_runtime_detail=? WHERE ticket_id=? AND archive_kind='triage_claim'", (original["source_claim_result_json"], original["source_runtime_detail"], self.ticket_id))

    def test_valid_archive_only_replay_preserves_triage_archive_and_preview_process_parity(self) -> None:
        self._prepare_archive_only_replay_state()
        before_archives = [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id", (self.ticket_id,)).fetchall()]
        replayed = self._attempt_matrix_recovery()
        self.assertEqual(replayed["state"], CanonicalState.LOCAL_REVIEW.value)
        after_archives = [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id", (self.ticket_id,)).fetchall()]
        self.assertEqual(before_archives, after_archives)
        self.ledger.resume("operator", reason="archive replay parity")
        review_model = RecoveryReviewModel()
        review_controller = LocalFirstController(self.ledger, self._production_triage_board(), self.controller.config, local_model=review_model)
        scheduler = ProcessNextScheduler(self.ledger, self._production_triage_board(), worker_id="archive-parity", target_ticket_id=self.ticket_id, lease_seconds=60, clock=lambda: 300, review_runner=lambda ticket_id: review_controller.execute_fresh_review_only(ticket_id, repository=self.repo), review_execution_policy_hash=review_controller.review_execution_policy_hash())
        for _ in range(6):
            preview = preview_next(self.ledger, now=300)
            result = scheduler.process_next()
            self.assertEqual(preview.next_stage, result.stage)
            if result.stage == "review": break
        else: self.fail("archive-only replay did not reach review with preview/process parity")

    def test_exact_recovery_replay_keeps_downstream_archives_byte_identical(self) -> None:
        self._prepare_stale_validation_recovery(with_triage_claim=True)
        self._attempt_matrix_recovery()
        before = [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM validation_recovery_archives WHERE ticket_id=? ORDER BY archive_id", (self.ticket_id,)).fetchall()]
        replayed = self._signed_recover(1, "matrix validator correction")
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
        self.ledger.connection.execute("UPDATE scheduler_stage_claims SET candidate_identity_json='{bad json' WHERE ticket_id=? AND stage='triage:1'", (self.ticket_id,))
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

    def _assert_post_validation_candidate_fault(self, mutator) -> None:
        self._prepare_stale_validation_recovery()
        reason = "validator handoff identity fault"
        document, signature = self._signed_recovery_material(1, reason)
        original_validate = DeterministicValidator.validate

        def validating_then_mutating(validator, *args, **kwargs):
            result = original_validate(validator, *args, **kwargs)
            self.assertTrue(result.passed, result.compact_evidence)
            mutator()
            return result

        before = self.ledger.connection.serialize()
        with patch.object(DeterministicValidator, "validate", new=validating_then_mutating):
            with self.assertRaisesRegex(RuntimeError, "candidate changed during validation handoff"):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id,
                    1,
                    repository=self.repo,
                    operator_id="operator",
                    reason=reason,
                    approval_document=document,
                    detached_signature=signature,
                )
        self.assertEqual(before, self.ledger.connection.serialize())
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_recovery_handoff_rejects_candidate_byte_mutation(self) -> None:
        self._assert_post_validation_candidate_fault(
            lambda: (self.repo / "app.py").write_text('def value():\n    return "mutated-after-validation"\n', encoding="utf-8")
        )

    def test_recovery_handoff_rejects_candidate_rename(self) -> None:
        self._assert_post_validation_candidate_fault(
            lambda: (self.repo / "app.py").rename(self.repo / "renamed-app.py")
        )

    def test_recovery_handoff_rejects_candidate_symlink_swap(self) -> None:
        def swap() -> None:
            target = self.root / "same-app.py"
            target.write_bytes((self.repo / "app.py").read_bytes())
            (self.repo / "app.py").unlink()
            (self.repo / "app.py").symlink_to(target)
        self._assert_post_validation_candidate_fault(swap)

    def test_recovery_handoff_rejects_byte_identical_inode_replacement(self) -> None:
        def replace() -> None:
            path = self.repo / "app.py"
            replacement = self.repo / ".app.py.replacement"
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        self._assert_post_validation_candidate_fault(replace)

    def test_recovery_revokes_capability_after_preflight_failure_then_allows_valid_retry(self) -> None:
        self._prepare_stale_validation_recovery()
        reason = "cleanup after forced preflight failure"
        document, signature = self._signed_recovery_material(1, reason)
        failed = type("FailedValidation", (), {"passed": False, "compact_evidence": "forced preflight failure"})()
        with patch.object(DeterministicValidator, "validate", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "fresh validation still fails"):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                    approval_document=document, detached_signature=signature,
                )
        self.assertEqual(recovery_boundary._REGISTRY, {})
        recovered = self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
            approval_document=document, detached_signature=signature,
        )
        self.assertEqual(recovered["state"], CanonicalState.LOCAL_REVIEW.value)
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_recovery_capability_detects_repository_replacement_immediately_before_commit(self) -> None:
        self._prepare_stale_validation_recovery()
        reason = "repository inode replacement before commit"
        document, signature = self._signed_recovery_material(1, reason)
        held = self.root / "repo-held"
        replaced = False

        def injector(point: str) -> None:
            nonlocal replaced
            if point != "after_validation_recovery_downstream_archive" or replaced:
                return
            self.repo.rename(held)
            shutil.copytree(held, self.repo, symlinks=True)
            replaced = True

        before = self.ledger.connection.serialize()
        self.ledger.failure_injector = injector
        try:
            with self.assertRaisesRegex(PermissionError, "filesystem identity"):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                    approval_document=document, detached_signature=signature,
                )
        finally:
            self.ledger.failure_injector = None
            if replaced:
                shutil.rmtree(self.repo)
                held.rename(self.repo)
        self.assertEqual(before, self.ledger.connection.serialize())
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def _assert_post_snapshot_recovery_fault(self, mutator, *, restore=None) -> None:
        self._prepare_stale_validation_recovery()
        reason = "post snapshot mutation"
        document, signature = self._signed_recovery_material(1, reason)
        before = self.ledger.connection.serialize()
        prior = self.controller.fault_injector
        def injector(point: str) -> None:
            if point == "after_validation_recovery_capability_issue":
                mutator()
        self.controller.fault_injector = injector
        try:
            with self.assertRaises(PermissionError):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                    approval_document=document, detached_signature=signature,
                )
        finally:
            self.controller.fault_injector = prior
            if restore is not None:
                restore()
        self.assertEqual(before, self.ledger.connection.serialize())
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_recovery_post_snapshot_rejects_changed_file_content_mutation(self) -> None:
        self._assert_post_snapshot_recovery_fault(
            lambda: (self.repo / "app.py").write_text('def value():\n    return "after-final-snapshot"\n', encoding="utf-8")
        )

    def test_recovery_post_snapshot_rejects_byte_identical_changed_file_inode_replacement(self) -> None:
        def mutate() -> None:
            path = self.repo / "app.py"
            replacement = self.repo / ".app.py.post-snapshot"
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        self._assert_post_snapshot_recovery_fault(mutate)

    def test_recovery_post_snapshot_rejects_worktree_root_replacement(self) -> None:
        held = self.root / "repo-post-snapshot-held"
        replaced = False
        def mutate() -> None:
            nonlocal replaced
            self.repo.rename(held)
            shutil.copytree(held, self.repo, symlinks=True)
            replaced = True
        def restore() -> None:
            if replaced:
                shutil.rmtree(self.repo)
                held.rename(self.repo)
        self._assert_post_snapshot_recovery_fault(mutate, restore=restore)

    def test_recovery_post_snapshot_rejects_dot_git_replacement(self) -> None:
        dot_git = self.repo / ".git"
        held = self.root / "git-post-snapshot-held"
        replaced = False
        def mutate() -> None:
            nonlocal replaced
            dot_git.rename(held)
            shutil.copytree(held, dot_git, symlinks=True)
            replaced = True
        def restore() -> None:
            if replaced:
                shutil.rmtree(dot_git)
                held.rename(dot_git)
        self._assert_post_snapshot_recovery_fault(mutate, restore=restore)

    def test_recovery_post_snapshot_rejects_git_index_inode_replacement(self) -> None:
        index_path = Path(subprocess.run(("git", "rev-parse", "--git-path", "index"), cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip())
        if not index_path.is_absolute():
            index_path = self.repo / index_path
        def mutate() -> None:
            replacement = index_path.with_name(index_path.name + ".replacement")
            replacement.write_bytes(index_path.read_bytes())
            os.replace(replacement, index_path)
        self._assert_post_snapshot_recovery_fault(mutate)

    def _assert_config_commit_fence_fault(self, mutator, restore) -> None:
        self._prepare_stale_validation_recovery()
        reason = "config commit fence mutation"
        document, signature = self._signed_recovery_material(1, reason)
        before = self.ledger.connection.serialize()
        prior = self.ledger.failure_injector
        fired = False
        def injector(point: str) -> None:
            nonlocal fired
            if point == "after_validation_recovery_commit_guard_before_sqlite_commit" and not fired:
                fired = True
                mutator()
        self.ledger.failure_injector = injector
        try:
            with self.assertRaises(PermissionError):
                self.controller.recover_terminal_validation_controller_defect(
                    self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                    approval_document=document, detached_signature=signature,
                )
        finally:
            self.ledger.failure_injector = prior
            restore()
        self.assertTrue(fired)
        self.assertEqual(before, self.ledger.connection.serialize())
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_recovery_commit_fence_rejects_same_byte_operator_config_inode_replacement(self) -> None:
        original = self.operator_config_path.read_bytes()
        def mutate() -> None:
            replacement = self.operator_config_path.with_suffix(".replacement")
            replacement.write_bytes(original)
            os.replace(replacement, self.operator_config_path)
        self._assert_config_commit_fence_fault(mutate, lambda: self.operator_config_path.write_bytes(original))

    def test_recovery_commit_fence_rejects_signer_key_replacement(self) -> None:
        original = self.operator_config_path.read_bytes()
        attacker = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        attacker_fp = fingerprint_public_key(attacker)
        def mutate() -> None:
            payload = json.loads(original.decode("utf-8"))
            payload["operator_signing_public_key"] = base64.b64encode(attacker).decode("ascii")
            payload["operator_signing_key_fingerprint"] = attacker_fp
            self.operator_config_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self._assert_config_commit_fence_fault(mutate, lambda: self.operator_config_path.write_bytes(original))

    def test_recovery_issuer_rejects_ordinary_same_process_caller(self) -> None:
        with self.assertRaisesRegex(PermissionError, "controller call provenance"):
            self.controller._validation_recovery_issuer()

    def _capture_unconsumed_recovery_capability(self):
        self._prepare_stale_validation_recovery()
        reason = "capture boundary capability"
        document, signature = self._signed_recovery_material(1, reason)
        captured = {}
        def fake_recover(*args, **kwargs):
            captured.update(kwargs)
            return {"ticket_id": self.ticket_id, "attempt_number": 1, "state": CanonicalState.LOCAL_REVIEW.value}
        with patch.object(self.ledger, "recover_terminal_validation_controller_defect", side_effect=fake_recover), patch("local_first_orchestrator.controller.revoke_validation_recovery_capability", side_effect=lambda capability: None):
            self.controller.recover_terminal_validation_controller_defect(
                self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                approval_document=document, detached_signature=signature,
            )
        return captured["recovery_authority_capability"], captured["recovery_operation_id"], captured

    def _consume_captured_recovery_capability(self, capability, operation_id, captured, *, ledger_connection):
        record = recovery_boundary._REGISTRY.get(id(capability))
        if record is None:
            record = next(item for item in recovery_boundary._REGISTRY.values() if item.operation_id == operation_id)
        return recovery_boundary.validate_and_consume_validation_recovery_capability(
            capability,
            operation_id=operation_id,
            operation_kind="recover",
            operation_reason=record.reason,
            transaction_token=self.ledger._active_transaction_token,
            ledger_path=self.ledger.database,
            repository=self.repo,
            runtime_binding=self.ledger.runtime_binding(self.ticket_id),
            ticket_id=self.ticket_id,
            attempt_number=1,
            terminal_generation=record.terminal_generation,
            operator_id="operator",
            expected_archive_ids=record.expected_archive_ids,
            ledger_connection=ledger_connection,
            replay_validation_artifact_path=captured["replay_validation_artifact_path"],
            replay_validation_artifact_sha256=captured["replay_validation_artifact_sha256"],
            replay_compact_evidence=captured["replay_compact_evidence"],
            validation_policy_hash=self.ledger._validation_policy_hash(self.ledger.get_ticket(self.ticket_id)),
        )

    def test_recovery_capability_rejects_alternate_same_inode_connection(self) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        alternate = sqlite3.connect(self.ledger.database, isolation_level=None)
        try:
            with self.ledger._transaction():
                with self.assertRaisesRegex(PermissionError, "exact Ledger.connection"):
                    self._consume_captured_recovery_capability(capability, operation_id, captured, ledger_connection=alternate)
        finally:
            alternate.close()
            recovery_boundary.revoke_validation_recovery_capability(capability)

    def test_recovery_capability_rejects_none_connection(self) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        try:
            with self.ledger._transaction():
                with self.assertRaisesRegex(PermissionError, "exact Ledger.connection"):
                    self._consume_captured_recovery_capability(capability, operation_id, captured, ledger_connection=None)
        finally:
            recovery_boundary.revoke_validation_recovery_capability(capability)

    def test_recovery_capability_rejects_stale_connection_after_ledger_inode_replacement(self) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        ledger_path = self.ledger.database
        preserved = self.root / "ledger-preserved-for-stale-connection.db"
        replacement = self.root / "ledger-stale-replacement.db"
        os.link(ledger_path, preserved)
        shutil.copy2(ledger_path, replacement)
        os.replace(replacement, ledger_path)
        try:
            with self.ledger._transaction():
                with self.assertRaisesRegex(PermissionError, "filesystem identity"):
                    self._consume_captured_recovery_capability(capability, operation_id, captured, ledger_connection=self.ledger.connection)
        finally:
            recovery_boundary.revoke_validation_recovery_capability(capability)
            ledger_path.unlink()
            os.link(preserved, ledger_path)
            preserved.unlink()

    def test_recovery_capability_rejects_copy_and_replay(self) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        try:
            import copy
            copied = copy.copy(capability)
            with self.ledger._transaction():
                with self.assertRaises(PermissionError):
                    self._consume_captured_recovery_capability(copied, operation_id, captured, ledger_connection=self.ledger.connection)
                verified = self._consume_captured_recovery_capability(capability, operation_id, captured, ledger_connection=self.ledger.connection)
                self.assertEqual(verified["operation_id"], operation_id)
                with self.assertRaisesRegex(PermissionError, "inactive or unregistered"):
                    self._consume_captured_recovery_capability(capability, operation_id, captured, ledger_connection=self.ledger.connection)
        finally:
            recovery_boundary.revoke_validation_recovery_capability(capability)

    def test_recovery_transaction_rejects_signed_authority_drift_from_raw_sql_without_mutation(self) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        record = recovery_boundary._REGISTRY[id(capability)]
        terminal = self.ledger.connection.execute(
            "SELECT failure_fingerprint FROM terminal_ticket_failures WHERE ticket_id=? AND generation=1",
            (self.ticket_id,),
        ).fetchone()
        original_fp = str(terminal["failure_fingerprint"])
        self.ledger.connection.execute(
            "UPDATE terminal_ticket_failures SET failure_fingerprint=? WHERE ticket_id=? AND generation=1",
            ("f" * 64, self.ticket_id),
        )
        before = self.ledger.connection.serialize()
        try:
            with self.assertRaisesRegex(PermissionError, "signed validation recovery authority"):
                self.ledger.recover_terminal_validation_controller_defect(
                    self.ticket_id,
                    attempt_number=1,
                    operator_id="operator",
                    reason=record.reason,
                    archived_validation_artifact_path=captured["archived_validation_artifact_path"],
                    archived_validation_artifact_sha256=captured["archived_validation_artifact_sha256"],
                    replay_validation_artifact_path=captured["replay_validation_artifact_path"],
                    replay_validation_artifact_sha256=captured["replay_validation_artifact_sha256"],
                    replay_compact_evidence=captured["replay_compact_evidence"],
                    recovery_authority_capability=capability,
                    recovery_operation_id=operation_id,
                )
            self.assertEqual(before, self.ledger.connection.serialize())
        finally:
            recovery_boundary.revoke_validation_recovery_capability(capability)
            self.ledger.connection.execute(
                "UPDATE terminal_ticket_failures SET failure_fingerprint=? WHERE ticket_id=? AND generation=1",
                (original_fp, self.ticket_id),
            )
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_marker_reconciliation_rejects_reason_different_from_signed_authority_without_mutation(self) -> None:
        authority = self._prepare_marker_reconciliation_state()
        signed = json.loads(str(authority["document_json"]))
        signed_reason = str(signed["reason"])
        self.assertNotEqual(signed_reason, "different audit reason")
        before = self.ledger.connection.serialize()
        with self.assertRaisesRegex(PermissionError, "immutable operation identity"):
            self.controller.reconcile_validation_controller_defect_completion_marker(
                self.ticket_id, 1, operator_id="operator", reason="different audit reason"
            )
        self.assertEqual(before, self.ledger.connection.serialize())
        self.assertEqual(recovery_boundary._REGISTRY, {})

    def test_recovery_git_identity_ignores_hostile_environment_global_config_and_fsmonitor(self) -> None:
        self._prepare_stale_validation_recovery()
        reason = "git hardening regression"
        document, signature = self._signed_recovery_material(1, reason)
        hostile = self.root / "hostile-git"
        hostile.mkdir()
        marker = self.root / "fsmonitor-ran"
        helper = self.root / "fsmonitor-helper.sh"
        helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
        helper.chmod(0o700)
        global_config = self.root / "hostile.gitconfig"
        global_config.write_text(
            f'[core]\n\tfsmonitor = {helper}\n\thooksPath = {hostile}\n', encoding="utf-8"
        )
        hostile_env = {
            "GIT_DIR": str(hostile),
            "GIT_WORK_TREE": str(hostile),
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_SYSTEM": str(global_config),
        }
        with patch.dict(os.environ, hostile_env, clear=False):
            recovered = self.controller.recover_terminal_validation_controller_defect(
                self.ticket_id, 1, repository=self.repo, operator_id="operator", reason=reason,
                approval_document=document, detached_signature=signature,
            )
        self.assertEqual(recovered["state"], CanonicalState.LOCAL_REVIEW.value)
        self.assertFalse(marker.exists())

    def _assert_terminal_field_drift_rejected(self, assignment: str, params: tuple[object, ...]) -> None:
        capability, operation_id, captured = self._capture_unconsumed_recovery_capability()
        record = recovery_boundary._REGISTRY[id(capability)]
        self.ledger.connection.execute(f"UPDATE terminal_ticket_failures SET {assignment} WHERE ticket_id=?", (*params, self.ticket_id))
        before = self.ledger.connection.serialize()
        try:
            with self.assertRaises((PermissionError, ValueError)):
                self.ledger.recover_terminal_validation_controller_defect(
                    self.ticket_id, attempt_number=1, operator_id="operator", reason=record.reason,
                    archived_validation_artifact_path=captured["archived_validation_artifact_path"],
                    archived_validation_artifact_sha256=captured["archived_validation_artifact_sha256"],
                    replay_validation_artifact_path=captured["replay_validation_artifact_path"],
                    replay_validation_artifact_sha256=captured["replay_validation_artifact_sha256"],
                    replay_compact_evidence=captured["replay_compact_evidence"],
                    recovery_authority_capability=capability, recovery_operation_id=operation_id,
                )
            self.assertEqual(before, self.ledger.connection.serialize())
        finally:
            recovery_boundary.revoke_validation_recovery_capability(capability)

    def test_recovery_rejects_terminal_resolved_at_drift(self) -> None:
        self._assert_terminal_field_drift_rejected("resolved_at=?", (123456,))

    def test_recovery_rejects_terminal_resolved_by_drift(self) -> None:
        self._assert_terminal_field_drift_rejected("resolved_by=?", ("raw-sql-writer",))

    def test_recovery_rejects_terminal_resolution_reason_drift(self) -> None:
        self._assert_terminal_field_drift_rejected("resolution_reason=?", ("forged resolution",))

    def test_recovery_rejects_terminal_reason_drift(self) -> None:
        self._assert_terminal_field_drift_rejected("reason=?", ("forged terminal reason",))

    def test_recovery_rejects_terminal_summary_drift(self) -> None:
        self._assert_terminal_field_drift_rejected("summary_json=?", ('{"forged":true}',))

    def test_recovery_commit_fence_rejects_changed_file_content(self) -> None:
        path = self.repo / "app.py"
        original = path.read_bytes()
        self._assert_config_commit_fence_fault(
            lambda: path.write_text('def value():\n    return "commit-race"\n', encoding="utf-8"),
            lambda: path.write_bytes(original),
        )

    def test_recovery_commit_fence_rejects_unauthorized_untracked_path(self) -> None:
        path = self.repo / "late-untracked.txt"
        self._assert_config_commit_fence_fault(
            lambda: path.write_text("late", encoding="utf-8"),
            lambda: path.unlink(missing_ok=True),
        )

    def test_recovery_commit_fence_rejects_deleted_path_recreation(self) -> None:
        path = self.repo / "app.py"
        original = path.read_bytes()
        def recreate() -> None:
            path.unlink()
            path.write_bytes(original)
        self._assert_config_commit_fence_fault(recreate, lambda: path.write_bytes(original))

    def test_recovery_commit_fence_rejects_git_admin_metadata_mutation(self) -> None:
        path = self.repo / ".git" / "config"
        original = path.read_bytes()
        self._assert_config_commit_fence_fault(
            lambda: path.write_bytes(original + b"\n# late mutation\n"),
            lambda: path.write_bytes(original),
        )

    def test_recovery_commit_fence_rejects_unpinned_directory_entry(self) -> None:
        path = self.repo / "late" / "nested.txt"
        def mutate() -> None:
            path.parent.mkdir()
            path.write_text("late", encoding="utf-8")
        def restore() -> None:
            path.unlink(missing_ok=True)
            if path.parent.exists():
                path.parent.rmdir()
        self._assert_config_commit_fence_fault(mutate, restore)

    def test_generation_two_recovery_preparation_ignores_generation_one_authority(self) -> None:
        self._prepare_stale_validation_recovery()
        first = self._signed_recover(1, "generation one validator correction")
        self.assertEqual(first["terminal_generation"], 1)
        claim = self.ledger.connection.execute(
            "SELECT * FROM scheduler_stage_claims WHERE ticket_id=? AND stage='validation:1'",
            (self.ticket_id,),
        ).fetchone()
        self.assertIsNotNone(claim)
        identity = json.loads(str(claim["candidate_identity_json"]))
        second_artifact = self.root / "generation-two-false-validation.json"
        second_artifact.write_text(json.dumps({"errors": ["second obsolete validator false positive"]}, sort_keys=True), encoding="utf-8")
        second_sha = hashlib.sha256(second_artifact.read_bytes()).hexdigest()
        evidence = "second obsolete validator false positive"
        failed_record = {
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": evidence,
            "validation_artifact": str(second_artifact),
            "validation_artifact_sha256": second_sha,
            "review_diff": str(_isolated_candidate_diff(self.repo, self.base)["diff"]),
            "review_selected_files": {"app.py": str(_isolated_candidate_diff(self.repo, self.base)["diff"])},
        }
        encoded = json.dumps(failed_record, sort_keys=True, separators=(",", ":"))
        claim_result = json.dumps({
            "ticket_id": self.ticket_id,
            "candidate_identity": identity,
            "passed": False,
            "compact_evidence": evidence,
            "validation_artifact": str(second_artifact),
            "validation_artifact_sha256": second_sha,
            "replayed": False,
        }, sort_keys=True, separators=(",", ":"))
        self.ledger.connection.execute(
            "UPDATE runtime_stages SET detail=?,artifact_path=?,artifact_sha256=? WHERE ticket_id=? AND stage='validation-1'",
            (encoded, str(second_artifact), second_sha, self.ticket_id),
        )
        self.ledger.connection.execute("DELETE FROM runtime_stages WHERE ticket_id=? AND stage='validation_completed'", (self.ticket_id,))
        self.ledger.connection.execute(
            "UPDATE scheduler_stage_claims SET result_json=? WHERE claim_id=?",
            (claim_result, claim["claim_id"]),
        )
        second_fingerprint = _stable_scheduler_failure_fingerprint(self.ticket_id, "validation", evidence)
        self.ledger.connection.execute(
            "UPDATE tickets SET state=? WHERE id=?",
            (CanonicalState.VERIFYING.value, self.ticket_id),
        )
        self.ledger.record_terminal_unresolvable(
            self.ticket_id,
            attempt_number=1,
            failure_fingerprint=second_fingerprint,
            reason="second deterministic validation terminal",
            summary={"deterministic_failure": {"source": "validation", "attempt_number": 1, "failure_evidence": evidence}, "local_feedback": evidence},
            notification_target="mattermost:ops",
        )
        prepared = self.controller.prepare_validation_controller_defect_recovery(
            self.ticket_id, 1, operator_id="operator", reason="generation two validator correction"
        )
        self.assertEqual(prepared["document"]["authority"]["terminal_generation"], 2)
        document = prepared["document"]
        signature = self.signing_key.sign(canonical_validation_recovery_bytes(document))
        second = self.controller.recover_terminal_validation_controller_defect(
            self.ticket_id, 1, repository=self.repo, operator_id="operator", reason="generation two validator correction",
            approval_document=document, detached_signature=signature,
        )
        self.assertEqual(second["terminal_generation"], 2)
        generations = [row[0] for row in self.ledger.connection.execute(
            "SELECT terminal_generation FROM validation_recovery_authorities WHERE ticket_id=? ORDER BY terminal_generation",
            (self.ticket_id,),
        ).fetchall()]
        self.assertEqual(generations, [1, 2])


if __name__ == "__main__":
    unittest.main()
