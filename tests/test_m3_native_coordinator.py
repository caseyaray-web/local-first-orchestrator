"""M3 coordinator integration against a pinned, disposable Hermes board.

The native lifecycle fixture below uses Hermes' supported Python kanban APIs only
for setup of completed source/reviewer runs.  It never stubs coordinator or
adapter provenance methods; the correction Action is produced by Coordinator.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import CandidateIdentity, ManagedMember
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter


IMPLEMENTER = "implementer"
REVIEWER = "local-review"


@pytest.fixture
def native_board(tmp_path):
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to the pinned installed Hermes executable")
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    board = "m3native"
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_KANBAN_BOARD=board)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_KANBAN_LOGS_ROOT", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        env.pop(key, None)

    def cli(*args, ok=True):
        result = subprocess.run([executable, "kanban", "--board", board, *args], env=env,
                                text=True, capture_output=True, timeout=30, check=False)
        if ok and result.returncode:
            pytest.fail(f"native CLI {args!r} failed: {result.stdout}\n{result.stderr}")
        return result

    cli("boards", "create", board)
    return executable, board, home, env, cli


def _create(cli, title, *, assignee, workspace):
    return json.loads(cli("create", title, "--assignee", assignee, "--workspace", f"dir:{workspace}", "--json").stdout)["id"]


def _complete_with_native_run(executable, env, board, task_id, session_id):
    """Supported native fixture lifecycle; no direct SQL or production stubs."""
    script = f'''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect(board={board!r}) as conn:
    claimed = kb.claim_task(conn, {task_id!r}, claimer="m3-native-fixture")
    assert claimed is not None and claimed.current_run_id is not None, claimed
    assert kb.complete_task(conn, {task_id!r}, result="fixture terminal evidence", expected_run_id=claimed.current_run_id,
                            metadata={{"worker_session_id": {session_id!r}}})
'''
    python = str(Path(executable).with_name("python"))
    result = subprocess.run([python, "-c", script], env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def _candidate(source_run_id, worktree):
    return CandidateIdentity("fixture-repository", worktree, "fixture-base", "fixture-head", "fixture-content",
                             "fixture-diff", str(source_run_id), "fixture-contract")


def _checks_identity(checks):
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _review(candidate, review_task_id, review_run_id):
    checks = [{"check_id": "native-fixture", "outcome": "passed", "evidence": "native lifecycle staged"}]
    return {
        "review_id": "native-review-1", "candidate_identity": candidate.to_dict(), "reviewer_role": "local",
        "native_review": {"task_id": review_task_id, "run_id": str(review_run_id), "session_id": "review-session", "profile": REVIEWER},
        "checks": checks,
        "checks_identity": _checks_identity(checks),
        "criterion_evidence": [{"criterion_id": "native-fixture", "outcome": "fail", "evidence": "finding retained"}],
        "verdict": "changes_requested",
        "findings": [{"finding_id": "native-finding-1", "criterion_id": "native-fixture", "severity": "major", "summary": "repair the retained finding"}],
    }


def _native_run_id(adapter, task_id):
    runs = adapter.read_task(task_id).runs
    assert len(runs) == 1
    return str(runs[0]["id"])


def _coordinator(tmp_path, executable, board_id, home, anchor, store, candidate):
    adapter = HermesBoardAdapter(
        board=board_id, anchor_task_id=anchor, executable=executable, hermes_home=home, kanban_home=home,
        managed_member_lookup=lambda scoped, task_id: any(
            member.task_id == task_id for member in store.read_scope(scoped)["members"]
        ),
    )
    scope = {"board_id": board_id, "anchor_task_id": anchor}
    checks = [{"check_id": "native-fixture", "outcome": "passed", "evidence": "native lifecycle staged"}]
    controller = Coordinator(
        scope, board=adapter, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
        git_observer=lambda _scope: {
            "candidate": candidate.to_dict(),
            "checks": [{"check_id": "native-fixture", "outcome": "passed", "evidence": "native lifecycle staged"}],
            "checks_identity": _checks_identity(checks),
            "criterion_ids": ["native-fixture"],
        },
        budget_policy=BudgetPolicy(implementation_attempts=2, review_corrections=2,
                                   infrastructure_retries=2, workflow_repairs=2, paid_capacity=2),
        configured_roles={"implementation_profile": IMPLEMENTER, "local_review_profile": REVIEWER},
    )
    adapter.managed_member_lookup = lambda scoped, task_id: any(
        member.task_id == task_id for member in store.read_scope(scoped)["members"]
    )
    return controller, adapter, scope


def _staged_controller(tmp_path, native_board):
    executable, board_id, home, env, cli = native_board
    workspace = str(tmp_path)
    anchor = _create(cli, "anchor", assignee=IMPLEMENTER, workspace=workspace)
    source = _create(cli, "source", assignee=IMPLEMENTER, workspace=workspace)
    _complete_with_native_run(executable, env, board_id, source, "implement-session")
    source_runs = json.loads(cli("runs", source, "--json").stdout)
    assert len(source_runs) == 1
    candidate = _candidate(source_runs[0]["id"], workspace)
    database = tmp_path / "evidence.sqlite"
    store = EvidenceStore.open(database, create_new=True)
    store.migrate()
    controller, adapter, scope = _coordinator(tmp_path, executable, board_id, home, anchor, store, candidate)
    store.register_member(ManagedMember(board_id, anchor, source, "implementation", 0, ("native-finding-1",), "source"))
    created_review = controller.recover_premature_done(source, candidate, reviewer_profile=REVIEWER)
    assert created_review["outcome"] == "held", created_review
    review_task = created_review["task_id"]
    assert controller.release_separate_review(review_task, candidate, reviewer_profile=REVIEWER)["outcome"] == "released"
    _complete_with_native_run(executable, env, board_id, review_task, "review-session")
    review = _review(candidate, review_task, _native_run_id(adapter, review_task))
    # Fixture seed: durable reviewer finding based on the real completed native
    # review above. The target correction path calls its own real provenance
    # checks again; no coordinator/adapter provenance method is mocked.
    store.record_candidate(scope, candidate)
    store.record_review(scope, candidate, review)
    return controller, adapter, store, database, candidate, review_task, review, cli


def test_create_separate_correction_uses_real_generated_action_and_releases_parentless_card(tmp_path, native_board):
    controller, adapter, store, _, candidate, review_task, review, _ = _staged_controller(tmp_path, native_board)
    created = controller.create_separate_correction(review_task, candidate, review)
    assert created["outcome"] == "held", created
    correction = adapter.read_task(created["task_id"])
    assert correction.native_task["status"] == "blocked"
    assert correction.parents == ()
    member = next(item for item in store.read_scope(controller.scope)["members"] if item.task_id == created["task_id"])
    assert member.role == "implementation" and member.work_association.startswith("separate-review-correction:")
    released = controller.tick()
    assert released == {"outcome": "released", "actions_attempted": 1, "task_id": created["task_id"]}
    assert adapter.read_task(created["task_id"]).native_task["status"] == "ready"
    store.close()


def test_lost_create_response_restart_reconciles_real_marker_without_duplicate(tmp_path, native_board, monkeypatch):
    controller, adapter, store, database, candidate, review_task, review, cli = _staged_controller(tmp_path, native_board)
    real_runner = adapter.runner
    lost = {"done": False}

    def lose_only_create(argv, **kwargs):
        result = real_runner(argv, **kwargs)
        if not lost["done"] and "create" in argv:
            lost["done"] = True
            return subprocess.CompletedProcess(argv, 1, result.stdout, "fixture lost create response")
        return result

    adapter.runner = lose_only_create
    first = controller.create_separate_correction(review_task, candidate, review)
    assert first == {"outcome": "held", "reason": "correction_create_unverified"}
    operations = [item for item in store.read_scope(controller.scope)["operations"] if item.effect == "create_held" and item.target.get("correction_of") == review_task]
    assert len(operations) == 1 and operations[0].phase == "unknown"
    before_restart = [row for row in json.loads(cli("list", "--archived", "--json").stdout) if row["title"].startswith("Correction:")]
    assert len(before_restart) == 1
    store.close()

    restarted = EvidenceStore.open(database)
    replacement, _, _ = _coordinator(tmp_path, native_board[0], native_board[1], native_board[2], controller.scope["anchor_task_id"], restarted, candidate)
    recovered = replacement.create_separate_correction(review_task, candidate, review)
    assert recovered["outcome"] == "held" and recovered["task_id"] == before_restart[0]["id"]
    after_restart = [row for row in json.loads(cli("list", "--archived", "--json").stdout) if row["title"].startswith("Correction:")]
    assert [row["id"] for row in after_restart] == [before_restart[0]["id"]]
    restarted.close()


def test_archived_lost_create_marker_conflicts_without_resend(tmp_path, native_board):
    controller, adapter, store, database, candidate, review_task, review, cli = _staged_controller(tmp_path, native_board)
    real_runner = adapter.runner
    sent = {"create": 0}

    def lose_only_create(argv, **kwargs):
        result = real_runner(argv, **kwargs)
        if "create" in argv:
            sent["create"] += 1
            return subprocess.CompletedProcess(argv, 1, result.stdout, "fixture lost create response")
        return result

    adapter.runner = lose_only_create
    assert controller.create_separate_correction(review_task, candidate, review) == {"outcome": "held", "reason": "correction_create_unverified"}
    correction = next(row for row in json.loads(cli("list", "--archived", "--json").stdout) if row["title"].startswith("Correction:"))
    cli("archive", correction["id"])
    store.close()

    restarted = EvidenceStore.open(database)
    replacement, _, _ = _coordinator(tmp_path, native_board[0], native_board[1], native_board[2], controller.scope["anchor_task_id"], restarted, candidate)
    assert replacement.create_separate_correction(review_task, candidate, review) == {"outcome": "held", "reason": "durable_correction_create_unknown"}
    assert sent["create"] == 1
    all_corrections = [row for row in json.loads(cli("list", "--archived", "--json").stdout) if row["title"].startswith("Correction:")]
    assert [row["id"] for row in all_corrections] == [correction["id"]]
    restarted.close()
