from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, CandidateIdentity, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import serialize_proposal
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.git_adapter import GitWorktreeAdapter
from local_first_orchestrator.planning_coordinator import request_payload
from tests.test_m4_plan_evidence import proposal, request
from tests.test_m4_planner_creation import native_fixture as _base_native_fixture
from tests.test_m4_planner_run_binding import _claim


def _git(path: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=path, text=True, capture_output=True,
                          check=True).stdout.strip()


def _checks_identity(checks):
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def native_paid_fixture(tmp_path):
    # This is deliberately before *any* filesystem/board fixture setup.  A child
    # can collect the exact node but cannot create, unblock, or inspect a fixture.
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        pytest.skip("native paid-review fixture mutations require the authorized parent context")
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to the pinned installed Hermes executable")
    return _base_native_fixture.__wrapped__(tmp_path)


def _accepted_native_plan(tmp_path, native_paid_fixture, monkeypatch, base_sha):
    board, anchor, workspace, adapter, membership, _cli = native_paid_fixture
    scope = {"board_id": board, "anchor_task_id": anchor}
    req = dataclasses.replace(request(), board_id=board, anchor_id=anchor, base_sha=base_sha)
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True)
    store.migrate()
    store.register_member(ManagedMember(board, anchor, anchor, "root", 0, (), "native-root"))
    controller = Coordinator(
        scope, board=adapter, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
        budget_policy=BudgetPolicy(3, 3, 3, 3, 3),
        configured_roles={"implementation_profile": "implementer", "local_review_profile": "local",
                          "paid_review_profile": "paid", "planning_profile": "planner"},
        planning_observer=lambda _scope: {"request": request_payload(req)},
        planning_profile="planner", planning_workspace=workspace,
    )
    membership["store"] = store
    with controller.lock:
        anchor_snapshot = adapter.read_task(anchor)
        held = controller._apply(Action("native-paid-root-hold:" + anchor_snapshot.digest, scope,
            {"task_id": anchor}, "hold", anchor_snapshot.digest))
        assert held.outcome in {"verified", "no-op"}
    planner = controller.prepare_planner(request_id="native-paid-plan")
    assert controller.release_planner(request_id="native-paid-plan")["outcome"] == "released"
    run_id = _claim(board, planner["task_id"], tmp_path / "home")
    monkeypatch.setenv("HERMES_KANBAN_TASK", planner["task_id"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    monkeypatch.setenv("HERMES_SESSION_ID", "native-paid-plan-session")
    controller.register_planning_request(planner["task_id"], request_id="native-paid-plan")
    controller.submit_plan(serialize_proposal(proposal(req)), request_id="native-paid-plan")
    accepted = controller.accept_validated_plan(proposal(req).plan.plan_id, request_id="native-paid-plan")
    return controller, store, scope, accepted["plan_id"], adapter


def test_native_paid_review_and_correction_are_parentless_and_admitted_once(
    tmp_path, native_paid_fixture, monkeypatch
):
    """Parent-only native characterization; review evidence below is explicitly synthetic."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Native Fixture")
    _git(repo, "config", "user.email", "native-fixture@example.invalid")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    controller, store, scope, plan_id, adapter = _accepted_native_plan(
        tmp_path, native_paid_fixture, monkeypatch, base
    )
    try:
        # Build one real disposable Git ref advance and persist its single durable
        # chain receipt.  The review/card work below is not a provider or worker run.
        git = GitWorktreeAdapter(repo, tmp_path / "attempts")
        git.resolve_execution_base("TR-A", base)
        (repo / "integrated.txt").write_text("integrated\n")
        _git(repo, "add", "integrated.txt")
        _git(repo, "commit", "-qm", "integrated")
        integrated_head = _git(repo, "rev-parse", "HEAD")
        assert git.advance_integration_head("TR-A", base, integrated_head) == integrated_head
        integration = OperationIntent(
            "native-paid-fixture-integration", scope,
            {"plan_id": plan_id, "ticket_id": "TK-A", "candidate": {"head_sha": integrated_head}},
            "git_integrate", base, {}, "verified",
            {"integration_head_before": base, "integration_head_after": integrated_head}, {}, "applied",
        )
        store.reserve_operation(integration)
        controller.combined_check_runner = lambda context: {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "offline-fixture", "command": "synthetic trusted check",
                        "exit_code": 0, "output_sha256": "sha256:" + context["head_sha"]}],
        }

        native_release = adapter.release
        release_phases = []

        def assert_claimed_then_release(action, task_id, reason):
            operation = next(op for op in store.read_scope(scope)["operations"] if op.key == action.key)
            release_phases.append((action.effect, operation.phase, task_id, reason))
            assert operation.phase == "unknown"
            return native_release(action, task_id, reason)

        adapter.release = assert_claimed_then_release
        paid = controller.prepare_paid_integrated_review(plan_id, git_adapter=git)
        assert paid["outcome"] == "held" and paid["actions_attempted"] == 1
        paid_create = next(op for op in store.read_scope(scope)["operations"] if op.key == paid["review_request_key"])
        assert paid_create.target == {
            "kind": "paid_integrated_review_v1", "anchor_task_id": scope["anchor_task_id"],
            "plan_id": plan_id, "tranche_id": "TR-A", "head_sha": integrated_head,
            "check_operation_key": paid_create.target["check_operation_key"],
            "candidate": paid_create.target["candidate"], "profile": "paid", "native_parent": False,
        }
        paid_held = adapter.read_task(paid["review_task_id"])
        assert paid_held.native_task["status"] == "blocked"
        assert paid_held.native_task["assignee"] == "paid"
        assert not paid_held.parents and not paid_held.runs

        paid_release = controller.release_paid_integrated_review(plan_id, git_adapter=git)
        assert paid_release["outcome"] == "released" and paid_release["actions_attempted"] == 1
        paid_ready = adapter.read_task(paid["review_task_id"])
        assert paid_ready.native_task["status"] in {"ready", "todo"}
        assert not paid_ready.parents and not paid_ready.runs
        paid_release_op = next(op for op in store.read_scope(scope)["operations"] if op.key == paid_release["operation_key"])
        assert paid_release_op.phase == "applied" and paid_release_op.readback["digest"] == paid_ready.digest
        assert len([event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]) == 2  # planner + paid review

        # This record is controlled synthetic review input, not an observed native
        # reviewer run.  Creation and release of the resulting correction remain
        # real Coordinator -> HermesBoardAdapter -> pinned-CLI effects.
        candidate = CandidateIdentity.from_dict(paid_create.readback["candidate"])
        checks = [{"check_id": "offline-fixture", "outcome": "passed", "evidence": "synthetic"}]
        changes = {
            "review_id": "synthetic-paid-changes", "candidate_identity": candidate.to_dict(),
            "reviewer_role": "paid",
            "native_review": {"task_id": paid["review_task_id"], "run_id": "synthetic-paid-run",
                              "session_id": "synthetic-paid-session", "profile": "paid"},
            "checks": checks, "checks_identity": _checks_identity(checks), "verdict": "changes_requested",
            "criterion_evidence": [{"criterion_id": "AC-1", "outcome": "fail", "evidence": "synthetic"}],
            "findings": [{"finding_id": "finding-native-1", "criterion_id": "AC-1", "severity": "major",
                          "summary": "synthetic fixture finding"}],
        }
        store.record_review(scope, candidate, changes)
        correction = controller.prepare_paid_correction(plan_id, "synthetic-paid-changes", git_adapter=git)
        assert correction["outcome"] == "held" and correction["actions_attempted"] == 1
        correction_create = next(op for op in store.read_scope(scope)["operations"] if op.key == correction["operation_key"])
        assert correction_create.target["kind"] == "paid_correction_v1"
        assert correction_create.target["native_parent"] is False
        assert correction_create.target["head_sha"] == integrated_head
        assert correction_create.target["finding_ids"] == ("finding-native-1",)
        correction_held = adapter.read_task(correction["correction_task_id"])
        assert correction_held.native_task["status"] == "blocked"
        assert correction_held.native_task["assignee"] == "implementer"
        assert not correction_held.parents and not correction_held.runs

        correction_release = controller.release_paid_correction(plan_id, "synthetic-paid-changes", git_adapter=git)
        assert correction_release["outcome"] == "released" and correction_release["actions_attempted"] == 1
        correction_ready = adapter.read_task(correction["correction_task_id"])
        assert correction_ready.native_task["status"] in {"ready", "todo"}
        assert not correction_ready.parents and not correction_ready.runs
        correction_release_op = next(op for op in store.read_scope(scope)["operations"] if op.key == correction_release["operation_key"])
        assert correction_release_op.phase == "applied"
        assert correction_release_op.target["plan_id"] == plan_id
        assert correction_release_op.target["ticket_id"] == correction["operation_key"]
        assert correction_release_op.readback["digest"] == correction_ready.digest
        assert [phase for effect, phase, _task, _reason in release_phases] == ["unknown", "unknown"]
        assert release_phases[0][0] == release_phases[1][0] == "release"
    finally:
        store.close()
