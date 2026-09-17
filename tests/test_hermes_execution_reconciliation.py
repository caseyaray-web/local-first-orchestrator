from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.execution_handoff import HANDOFF_MARKER, HANDOFF_SENTINEL
from local_first_orchestrator.hermes_board import ExternalExecutionRun, ExternalExecutionSnapshot, ExternalTicket
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler
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
