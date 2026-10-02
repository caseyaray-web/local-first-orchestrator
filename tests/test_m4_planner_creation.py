from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import ConflictError
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import request_payload
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.contracts import PauseIntent
from tests.test_m4_plan_evidence import request


@pytest.fixture
def native_fixture(tmp_path):
    if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
        pytest.skip("native fixture mutations require the authorized parent context")
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to pinned Hermes CLI")
    executable = str(Path(executable).resolve())
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    board = "m4plannercreate"
    env = os.environ.copy(); env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_KANBAN_BOARD=board)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_LOGS_ROOT", "HERMES_PROFILE", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        env.pop(key, None)
    def cli(*args):
        result = subprocess.run([executable, "kanban", "--board", board, *args], env=env, cwd=tmp_path, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    cli("boards", "create", board)
    workspace = str(tmp_path)
    anchor = json.loads(cli("create", "anchor", "--assignee", "default", "--workspace", f"dir:{workspace}", "--json").stdout)["id"]
    membership = {"store": None}
    def is_member(scoped, task_id):
        store = membership["store"]
        return (scoped == {"board_id": board, "anchor_task_id": anchor} and store is not None
                and any(member.task_id == task_id for member in store.read_scope(scoped)["members"]))
    adapter = HermesBoardAdapter(board=board, anchor_task_id=anchor, executable=executable, hermes_home=home, kanban_home=home,
                                 managed_member_lookup=is_member)
    return board, anchor, workspace, adapter, membership, cli


def setup(tmp_path, native_fixture, *, observed=None, profile="planner"):
    board, anchor, workspace, adapter, membership, _cli = native_fixture
    scope = {"board_id": board, "anchor_task_id": anchor}
    req = dataclasses.replace(request(), board_id=board, anchor_id=anchor)
    db = tmp_path / "evidence.sqlite"
    store = EvidenceStore.open(db, create_new=True); store.migrate()
    from local_first_orchestrator.contracts import ManagedMember
    store.register_member(ManagedMember(board, anchor, anchor, "root", 0, (), "native-root"))
    controller = Coordinator(scope, board=adapter, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
        budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
        configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": profile},
        planning_observer=lambda _scope: {"request": request_payload(req) if observed is None else observed["request"]}, planning_profile=profile,
        planning_workspace=workspace)
    membership["store"] = store
    from local_first_orchestrator.contracts import Action
    with controller.lock:
        anchor_snapshot = adapter.read_task(anchor)
        held = controller._apply(Action("fixture-root-hold:" + anchor_snapshot.digest, scope,
            {"task_id": anchor}, "hold", anchor_snapshot.digest))
        assert held.outcome in {"verified", "no-op"}
    return controller, store, scope, req


def test_prepare_planner_creates_one_held_card_and_reconciles_after_restart(tmp_path, native_fixture):
    controller, store, scope, req = setup(tmp_path, native_fixture)
    try:
        first = controller.prepare_planner(request_id="planning-request-1")
        task_id = first["task_id"]
        readback = controller.board.read_task(task_id)
        assert readback.native_task["status"] == "blocked"
        assert readback.native_task["assignee"] == "planner"
        assert readback.native_task.get("workspace_kind") == "dir"
        assert readback.native_task.get("workspace_path") == native_fixture[2]
        assert req.identity in readback.native_task["body"]
        assert not readback.parents
        member = next(m for m in store.read_scope(scope)["members"] if m.task_id == task_id)
        assert member.role == "planner" and member.work_association == req.identity
        count_before = len(controller.board.list_tasks())
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        controller.store = store
        replay = controller.prepare_planner(request_id="planning-request-1")
        assert replay == first
        assert len(controller.board.list_tasks()) == count_before
        with pytest.raises(ValueError, match="request_id"):
            controller.prepare_planner(request_id="planning-request-2")
        assert len(controller.board.list_tasks()) == count_before
        assert controller.prepare_planner(request_id="planning-request-1") == first
        assert len(controller.board.list_tasks()) == count_before
    finally:
        store.close()


def test_prepare_planner_rejects_stale_request_profile_and_active_pause(tmp_path, native_fixture):
    observed = {"request": None}
    controller, store, scope, req = setup(tmp_path, native_fixture, observed=observed)
    try:
        observed["request"] = request_payload(req)
        first = controller.prepare_planner(request_id="stale")
        observed["request"] = request_payload(dataclasses.replace(req, base_sha="f" * 40))
        with pytest.raises((ValueError, ConflictError)):
            controller.prepare_planner(request_id="stale")
        observed["request"] = request_payload(req)
        with pytest.raises((ValueError, ConflictError)):
            controller.prepare_planner(request_id="wrong-profile", planning_profile="other")
        store.set_operator_intent(PauseIntent(scope, "operator", 1, True, False))
        with pytest.raises((ValueError, ConflictError)):
            controller.prepare_planner(request_id="paused")
        assert [m for m in store.read_scope(scope)["members"] if m.role == "planner"] == [
            m for m in store.read_scope(scope)["members"] if m.work_association == req.identity]
    finally:
        store.close()


def test_lost_create_response_is_marker_reconciled_after_restart(tmp_path, native_fixture):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    adapter = controller.board
    real_runner = adapter.runner
    lost = {"once": True}

    def runner(argv, **kwargs):
        result = real_runner(argv, **kwargs)
        if "create" in argv and lost["once"]:
            lost["once"] = False
            return subprocess.CompletedProcess(argv, 1, "", "simulated lost create response")
        return result

    adapter.runner = runner
    try:
        with pytest.raises(ValueError, match="unverified"):
            controller.prepare_planner(request_id="lost-response")
        tasks = adapter.list_tasks()
        created = [task for task in tasks if task.native_task.get("title", "").startswith("Planning:")]
        assert len(created) == 1
        operation = next(op for op in store.read_scope(scope)["operations"] if op.effect == "create_held")
        assert operation.phase == "unknown"
        task_id = str(created[0].native_task["id"])
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        controller.store = store
        replay = controller.prepare_planner(request_id="lost-response")
        assert replay == {"outcome": "held", "task_id": task_id}
        assert len([task for task in adapter.list_tasks()
                    if task.native_task.get("title", "").startswith("Planning:")]) == 1
        assert next(op for op in store.read_scope(scope)["operations"] if op.effect == "create_held").phase == "applied"
        assert created[0].native_task.get("status") == "blocked"
        assert created[0].native_task.get("assignee") == "planner"
        assert created[0].native_task.get("workspace_kind") == "dir"
        assert created[0].native_task.get("workspace_path") == native_fixture[2]
        assert not created[0].parents
        assert "local-first-create:" in created[0].native_task.get("body", "")
        assert controller.tick()["outcome"] == "no-op"
        assert adapter.read_task(task_id).native_task.get("status") == "blocked"
        workflow = [event for event in store.read_scope(scope)["budget_events"]
                    if event.get("event_id", "").startswith("workflow_repairs:")]
        paid = [event for event in store.read_scope(scope)["budget_events"]
                if event.get("event_id", "").startswith("paid_capacity:")]
        assert len(workflow) == 1
        assert not paid
    finally:
        store.close()


@pytest.mark.parametrize("human_change", [False, True], ids=["unchanged", "native-comment-drift"])
def test_member_insert_crash_recovery_verifies_exact_native_card(tmp_path, native_fixture, human_change):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    original_register = store.register_member
    failed = {"once": False}

    def crash_after_native_ack(member):
        if member.role == "planner" and not failed["once"]:
            failed["once"] = True
            raise RuntimeError("simulated crash after native create acknowledgement")
        return original_register(member)

    store.register_member = crash_after_native_ack
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            controller.prepare_planner(request_id="member-crash")
        cards = [task for task in controller.board.list_tasks()
                 if task.native_task.get("title", "").startswith("Planning:")]
        assert len(cards) == 1
        task_id = str(cards[0].native_task["id"])
        assert not [m for m in store.read_scope(scope)["members"] if m.role == "planner"]
        store.close()
        if human_change:
            native_fixture[5]("comment", task_id, "human update after create acknowledgement")

        board, anchor, workspace, adapter, membership, _cli = native_fixture
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        req = dataclasses.replace(request(), board_id=board, anchor_id=anchor)
        controller = Coordinator(scope, board=adapter, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
            budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
            configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner"},
            planning_observer=lambda _scope: {"request": request_payload(req)}, planning_profile="planner", planning_workspace=workspace)
        membership["store"] = store
        count = len(adapter.list_tasks())
        paid_before = [e for e in store.read_scope(scope)["budget_events"] if e.get("event_id", "").startswith("paid_capacity:")]
        if human_change:
            with pytest.raises((ValueError, ConflictError)):
                controller.prepare_planner(request_id="member-crash")
            assert len(adapter.list_tasks()) == count
            assert not [m for m in store.read_scope(scope)["members"] if m.role == "planner"]
            assert [e for e in store.read_scope(scope)["budget_events"] if e.get("event_id", "").startswith("paid_capacity:")] == paid_before
        else:
            assert controller.prepare_planner(request_id="member-crash") == {"outcome": "held", "task_id": task_id}
            assert len(adapter.list_tasks()) == count
            assert len([m for m in store.read_scope(scope)["members"] if m.role == "planner"]) == 1
            assert not [e for e in store.read_scope(scope)["budget_events"] if e.get("event_id", "").startswith("paid_capacity:")]
            assert len([e for e in store.read_scope(scope)["budget_events"] if e.get("event_id", "").startswith("workflow_repairs:")]) == 1
    finally:
        store.close()
