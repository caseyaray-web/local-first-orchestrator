"""Native planner provenance and immutable evidence acceptance tests."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess

import pytest

from local_first_orchestrator.contracts import ConflictError, ManagedMember
from local_first_orchestrator.budgets import BudgetPolicy, GENERAL_ATTEMPT, PAID_CAPACITY
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import request_payload, serialize_proposal
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.planning_coordinator import evidence_payload
from tests.test_m4_plan_evidence import SCOPE, proposal, request


@pytest.fixture
def native_board(tmp_path):
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to the pinned installed Hermes executable")
    executable = str(Path(executable).resolve())
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    board = "m4planner"
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_KANBAN_BOARD=board)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_KANBAN_LOGS_ROOT", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        env.pop(key, None)
    def cli(*args):
        result = subprocess.run([executable, "kanban", "--board", board, *args], env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    cli("boards", "create", board)
    return executable, board, home, env, cli


def _native_task(tmp_path, native_board):
    executable, board, home, env, cli = native_board
    workspace = str(tmp_path)
    anchor = json.loads(cli("create", "anchor", "--assignee", "default", "--workspace", f"dir:{workspace}", "--json").stdout)["id"]
    task = json.loads(cli("create", "planner", "--assignee", "default", "--workspace", f"dir:{workspace}", "--json").stdout)["id"]
    script = f'''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect(board={board!r}) as conn:
    claimed = kb.claim_task(conn, {task!r}, claimer="m4-native-planner")
    assert claimed is not None and claimed.current_run_id is not None, claimed
    print(claimed.current_run_id)
'''
    python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    native_env = dict(env)
    native_env["PYTHONPATH"] = str(Path.home() / ".hermes" / "hermes-agent")
    result = subprocess.run([str(python), "-c", script], env=native_env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    run_id = result.stdout.strip().splitlines()[-1]
    adapter = HermesBoardAdapter(board=board, anchor_task_id=anchor, executable=executable, hermes_home=home, kanban_home=home,
                                 managed_member_lookup=lambda scoped, task_id: task_id == task)
    return anchor, task, run_id, adapter, cli


def _controller(tmp_path, store, adapter, anchor, task, run_id, req, *, observer=None, profile="default"):
    scope = {"board_id": adapter.board, "anchor_task_id": anchor}
    store.register_member(ManagedMember(adapter.board, anchor, task, "planner", 0, (), "planner"))
    controller = Coordinator(scope, board=adapter, store=store, lock=instance_lock(tmp_path / "coordinator.lock"),
                             budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
                             configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": profile},
                             planning_observer=observer or (lambda _scope: {"request": request_payload(req)}), planning_profile=profile)
    return controller, scope



def _released_fixture(tmp_path):
    from tests.test_m4_planner_creation import native_fixture, setup
    from tests.test_m4_planner_run_binding import _claim
    fixture = native_fixture.__wrapped__(tmp_path)
    controller, store, scope, req = setup(tmp_path, fixture, profile="default")
    task = controller.prepare_planner()["task_id"]
    assert controller.release_planner()["outcome"] == "released"
    run_id = _claim(scope["board_id"], task, tmp_path / "home")
    return controller, store, scope, req, task, run_id, fixture


def _paid_events(store, scope):
    return [event for event in store.read_scope(scope)["budget_events"]
            if event["event_id"].startswith(PAID_CAPACITY + ":")]

def test_native_running_planner_registration_and_submit_are_readback_bound(tmp_path, native_board, monkeypatch):
    controller, store, scope, req, task, run_id, fixture = _released_fixture(tmp_path)
    board, anchor, workspace, adapter, membership, cli = fixture
    try:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        monkeypatch.setenv("HERMES_SESSION_ID", "m4-active-session")
        registration = controller.register_planning_request(task)
        assert registration["planner_task_id"] == task and registration["planner_profile"] == "default"
        assert request_payload(req) == registration["request"]
        assert all(item.effect != "register_planning_request" for item in store.pending_operations(scope))
        assert controller.tick() == {"outcome": "no-op", "actions_attempted": 0}
        run = adapter.read_scoped_run(scope, task, run_id)
        assert str(run["id"]) == run_id
        assert run.get("profile") == "default"
        assert run.get("status") in {"running", "active"}
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        monkeypatch.setenv("HERMES_SESSION_ID", "m4-active-session")
        # Native running run does not promise a worker_session_id. The exact
        # active ENV session is the worker receipt, as in the M3 provenance pattern.
        serialized = serialize_proposal(proposal(req))
        evidence = controller.submit_plan(serialized)
        assert evidence["planner"] == {"task_id": task, "run_id": run_id, "session_id": "m4-active-session", "profile": "default"}
        plan_id = proposal(req).plan.plan_id
        assert store.read_plan(scope, plan_id) == evidence
        assert controller.submit_plan(serialized) == evidence
        altered = dataclasses.replace(proposal(req), tranche_semantics=(dataclasses.replace(proposal(req).tranche_semantics[0], objective="changed objective"), proposal(req).tranche_semantics[1]))
        with pytest.raises((ConflictError, ValueError)):
            controller.submit_plan(serialize_proposal(altered))
    finally:
        store.close()


def test_store_registration_replay_changed_request_and_multiple_explicit_ids(tmp_path):
    from tests.test_m4_plan_evidence import SCOPE
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    try:
        store.register_member(ManagedMember("board-A", "anchor-A", "p1", "planner", 0, (), "p1"))
        store.register_member(ManagedMember("board-A", "anchor-A", "p2", "planner", 0, (), "p2"))
        first = request()
        evidence = store.register_planning_request(SCOPE, first, "p1", "profile")
        assert store.read_planning_request(SCOPE) == evidence
        assert store.register_planning_request(SCOPE, first, "p1", "profile") == evidence
        with pytest.raises(ConflictError):
            store.register_planning_request(SCOPE, first, "p2", "profile")
        with pytest.raises(ConflictError):
            store.register_planning_request(SCOPE, dataclasses.replace(first, base_sha="d" * 40), "p1", "profile")
        revision = dataclasses.replace(first, base_sha="d" * 40)
        newer = store.register_planning_request(SCOPE, revision, "p1", "profile", request_id="revision-2")
        assert store.read_planning_request(SCOPE, request_id="revision-2") == newer
        assert store.read_planning_request(SCOPE) == evidence
    finally:
        store.close()


def test_submit_rejects_missing_or_wrong_worker_context_stale_observer_and_pause(tmp_path, native_board, monkeypatch):
    ctl, store, scope, req, task, run_id, fixture = _released_fixture(tmp_path)
    adapter = fixture[3]
    observed = {"request": request_payload(req)}
    ctl.planning_observer = lambda _: observed
    class BoardProxy:
        is_fake = True
        observed_run = adapter.read_scoped_run(scope, task, run_id)
        def read_task(self, task_id): return adapter.read_task(task_id)
        def _native_marker(self, action): return adapter._native_marker(action)
        def hold(self, *args, **kwargs): return adapter.hold(*args, **kwargs)
        def release(self, *args, **kwargs): return adapter.release(*args, **kwargs)
        def stop_run(self, *args, **kwargs): return adapter.stop_run(*args, **kwargs)
        def read_scoped_run(self, scope, task_id, rid): return dict(self.observed_run)
    board = BoardProxy()
    ctl.board = board
    try:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", "session")
        ctl.register_planning_request(task)
        raw = serialize_proposal(proposal(req))
        for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
            old = os.environ.pop(key)
            with pytest.raises((ValueError, ConflictError)):
                ctl.submit_plan(raw)
            monkeypatch.setenv(key, old)
        monkeypatch.setenv("HERMES_KANBAN_TASK", "wrong")
        with pytest.raises((ValueError, ConflictError)): ctl.submit_plan(raw)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        board.observed_run["profile"] = "wrong-profile"
        with pytest.raises((ValueError, ConflictError)): ctl.submit_plan(raw)
        board.observed_run["profile"] = "default"
        board.observed_run["status"] = "completed"
        with pytest.raises((ValueError, ConflictError)): ctl.submit_plan(raw)
        board.observed_run["status"] = "running"
        observed["request"] = request_payload(dataclasses.replace(req, base_sha="e" * 40))
        with pytest.raises((ValueError, ConflictError)): ctl.submit_plan(raw)
        observed["request"] = request_payload(req)
        from local_first_orchestrator.contracts import PauseIntent
        store.set_operator_intent(PauseIntent(scope, "operator", 1, False, False))
        with pytest.raises((ValueError, ConflictError)): ctl.submit_plan(raw)
    finally:
        store.close()


@pytest.mark.parametrize("policy", [None, BudgetPolicy(1, 1, 1, 1, 0)])
def test_registration_requires_paid_capacity_and_denial_writes_nothing(tmp_path, monkeypatch, policy):
    req = request(); task = "planner"; run_id = "run-1"
    class Board:
        is_fake = True
        def read_task(self, task_id): return {"id": task_id}
        def hold(self, *args, **kwargs): pass
        def release(self, *args, **kwargs): pass
        def stop_run(self, *args, **kwargs): pass
        def read_scoped_run(self, scope, task_id, rid): return {"id": rid, "task_id": task_id, "status": "running", "profile": "profile"}
    store = EvidenceStore.open(tmp_path / "denied.sqlite", create_new=True); store.migrate()
    try:
        store.register_member(ManagedMember("board-A", "anchor-A", task, "planner", 0, (), "planner"))
        ctl = Coordinator(SCOPE, board=Board(), store=store, lock=instance_lock(tmp_path / "lock"), budget_policy=policy,
                          configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "profile"},
                          planning_observer=lambda _: {"request": request_payload(req)}, planning_profile="profile")
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", "session")
        with pytest.raises((ValueError, ConflictError)):
            ctl.register_planning_request(task)
        state = store.read_scope(SCOPE)
        assert not state["budget_events"] and not state["operations"]
        with pytest.raises(KeyError):
            store.read_planning_request(SCOPE)
    finally:
        store.close()


@pytest.mark.parametrize("bad_roles,explicit_profile,expected", [
    ({}, "profile", "configured planning role"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer"}, "profile", "configured planning role"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": ""}, "profile", "configured roles"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "implementer"}, "implementer", "planning, implementation, and local-review roles"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "reviewer"}, "reviewer", "planning, implementation, and local-review roles"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "profile"}, "other-profile", "explicit planning profile does not match configured role"),
    ({"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "profile", "extra": "surplus"}, "profile", "configured planning role"),
])
@pytest.mark.parametrize("action", ["register", "submit"])
def test_invalid_planning_roles_reject_before_any_evidence_write(
        tmp_path, monkeypatch, bad_roles, explicit_profile, expected, action):
    req = request(); task = "planner"; run_id = "run-1"

    class Board:
        is_fake = True
        def read_task(self, task_id): return {"id": task_id}
        def hold(self, *args, **kwargs): pass
        def release(self, *args, **kwargs): pass
        def stop_run(self, *args, **kwargs): pass
        def read_scoped_run(self, scope, task_id, rid):
            return {"id": rid, "task_id": task_id, "status": "running", "profile": "profile"}

    store = EvidenceStore.open(tmp_path / f"invalid-roles-{action}.sqlite", create_new=True); store.migrate()
    try:
        store.register_member(ManagedMember("board-A", "anchor-A", task, "planner", 0, (), "planner"))
        if action == "submit":
            store.register_planning_request(SCOPE, req, task, "profile")
        ctl = Coordinator(SCOPE, board=Board(), store=store, lock=instance_lock(tmp_path / "lock"),
                          budget_policy=BudgetPolicy(2, 2, 2, 2, 1), configured_roles=bad_roles,
                          planning_observer=lambda _: {"request": request_payload(req)},
                          planning_profile=explicit_profile)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        monkeypatch.setenv("HERMES_SESSION_ID", "session")

        def evidence_state():
            state = store.read_scope(SCOPE)
            plan_id = proposal(req).plan.plan_id
            try:
                plan = store.read_plan(SCOPE, plan_id)
            except KeyError:
                plan = None
            try:
                registered = store.read_planning_request(SCOPE)
            except KeyError:
                registered = None
            return (state["budget_events"], state["operations"], registered, plan)

        before = evidence_state()
        with pytest.raises(ValueError, match=expected):
            if action == "register":
                ctl.register_planning_request(task)
            else:
                ctl.submit_plan(serialize_proposal(proposal(req)))
        assert evidence_state() == before
    finally:
        store.close()


def test_native_run_budget_charged_once_and_new_run_refused(tmp_path, native_board, monkeypatch):
    ctl, store, scope, req, task, run_id, fixture = _released_fixture(tmp_path)
    adapter = fixture[3]
    class BoardProxy:
        is_fake = True
        original_run = adapter.read_scoped_run(scope, task, run_id)
        def read_task(self, task_id): return adapter.read_task(task_id)
        def _native_marker(self, action): return adapter._native_marker(action)
        def hold(self, *args, **kwargs): return adapter.hold(*args, **kwargs)
        def release(self, *args, **kwargs): return adapter.release(*args, **kwargs)
        def stop_run(self, *args, **kwargs): return adapter.stop_run(*args, **kwargs)
        def read_scoped_run(self, scope, task_id, rid):
            result = dict(self.original_run)
            result.update({"id": rid, "task_id": task, "status": "running", "profile": "default"})
            return result
    ctl.board = BoardProxy()
    ctl.budget_policy = BudgetPolicy(1, 1, 1, 1, 1)
    try:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", "session")
        ctl.register_planning_request(task)
        events = _paid_events(store, scope)
        assert len(events) == 1
        assert events[0]["event_id"].startswith(f"{PAID_CAPACITY}:")
        assert events[0]["source_kind"] == "native_operation"
        assert events[0]["native_source_id"]
        assert events[0]["native_source_id"] != run_id
        revision = ctl.register_planning_request(task, request_id="revision-2")
        assert store.read_planning_request(scope, request_id="revision-2") == revision
        ctl.register_planning_request(task)
        ctl.submit_plan(serialize_proposal(proposal(req)))
        assert len(_paid_events(store, scope)) == 1
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "run-2")
        with pytest.raises((ValueError, ConflictError)):
            ctl.register_planning_request(task)
        assert len(_paid_events(store, scope)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("cancellation", [False, True])
def test_pause_and_cancel_block_registration_without_accounting_writes(tmp_path, monkeypatch, cancellation):
    from local_first_orchestrator.contracts import PauseIntent
    req = request(); task = "planner"; run_id = "run-1"
    class Board:
        is_fake = True
        def read_task(self, task_id): return {"id": task_id}
        def hold(self, *args, **kwargs): pass
        def release(self, *args, **kwargs): pass
        def stop_run(self, *args, **kwargs): pass
        def read_scoped_run(self, scope, task_id, rid): return {"id": rid, "task_id": task_id, "status": "running", "profile": "profile"}
    store = EvidenceStore.open(tmp_path / "fenced.sqlite", create_new=True); store.migrate()
    try:
        store.register_member(ManagedMember("board-A", "anchor-A", task, "planner", 0, (), "planner"))
        ctl = Coordinator(SCOPE, board=Board(), store=store, lock=instance_lock(tmp_path / "lock"), budget_policy=BudgetPolicy(1, 1, 1, 1, 1), configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "profile"},
                          planning_observer=lambda _: {"request": request_payload(req)}, planning_profile="profile")
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", "session")
        store.set_operator_intent(PauseIntent(SCOPE, "operator", 1, True, cancellation))
        with pytest.raises(ValueError):
            ctl.register_planning_request(task)
        state = store.read_scope(SCOPE)
        assert not state["budget_events"] and not state["operations"]
    finally:
        store.close()


def test_native_planner_restart_and_completion_lifecycle(tmp_path, native_board, monkeypatch):
    from local_first_orchestrator.contracts import PauseIntent

    ctl, store, scope, req, task, run_id, fixture = _released_fixture(tmp_path)
    board, anchor, workspace, adapter, membership, cli = fixture
    observed = {"request": request_payload(req)}
    db = tmp_path / "evidence.sqlite"
    session = "native-restart-session"
    env = dict(os.environ)
    env.update(HERMES_HOME=str(tmp_path / "home"), HERMES_KANBAN_HOME=str(tmp_path / "home"), HERMES_KANBAN_BOARD=board)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)

    def ctl_for(store, profile="default"):
        membership["store"] = store
        return Coordinator(scope, board=adapter, store=store,
            lock=instance_lock(tmp_path / "native-restart.lock"),
            budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
            configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": profile},
            planning_observer=lambda _scope: observed, planning_profile=profile,
            planning_workspace=workspace)

    def counts(store):
        state = store.read_scope(scope)
        return (len(_paid_events(store, scope)), len(state["operations"]),
                state.get("planning_requests"), state.get("plans"))

    try:
        ctl = ctl_for(store)
        initial = counts(store)
        for missing in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
            monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", session)
            monkeypatch.delenv(missing)
            with pytest.raises((ValueError, ConflictError, KeyError)): ctl.register_planning_request(task)
            assert counts(store) == initial
        monkeypatch.setenv("HERMES_KANBAN_TASK", "wrong-task"); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", session)
        with pytest.raises((ValueError, ConflictError, KeyError)): ctl.register_planning_request(task)
        assert counts(store) == initial
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "wrong-run")
        with pytest.raises((ValueError, ConflictError, KeyError)): ctl.register_planning_request(task)
        assert counts(store) == initial
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        with pytest.raises((ValueError, ConflictError)): ctl_for(store, "wrong-profile").register_planning_request(task)
        assert counts(store) == initial
        registration = ctl.register_planning_request(task)
        raw = serialize_proposal(proposal(req))
        assert registration["request"] == request_payload(req) and counts(store)[0] == 1

        def rejected():
            prior = counts(store)
            with pytest.raises((ValueError, ConflictError, KeyError)): ctl.submit_plan(raw)
            assert counts(store) == prior
        for missing in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
            monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", session)
            monkeypatch.delenv(missing); rejected()
        monkeypatch.setenv("HERMES_KANBAN_TASK", "wrong-task"); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", session); rejected()
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "wrong-run"); rejected()
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        prior = counts(store)
        with pytest.raises((ValueError, ConflictError)): ctl_for(store, "wrong-profile").submit_plan(raw)
        assert counts(store) == prior
        observed["request"] = request_payload(dataclasses.replace(req, base_sha="f" * 40)); rejected()
        observed["request"] = request_payload(req)
        monkeypatch.setenv("HERMES_KANBAN_TASK", task); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id); monkeypatch.setenv("HERMES_SESSION_ID", session)
        evidence = ctl.submit_plan(raw); plan_id = proposal(req).plan.plan_id
        assert store.read_plan(scope, plan_id) == evidence and counts(store)[0] == 1
        store.close()
        store = EvidenceStore.open(db); ctl = ctl_for(store)
        assert ctl.tick() == {"outcome": "no-op", "actions_attempted": 0}
        assert store.read_planning_request(scope) == registration and store.read_plan(scope, plan_id) == evidence
        assert ctl.submit_plan(raw) == evidence and counts(store)[0] == 1

        script = f'''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect(board={board!r}) as conn:
    assert kb.complete_task(conn, {task!r}, result="native planner fixture complete", expected_run_id={run_id!r}, metadata={{"worker_session_id": {session!r}}})
'''
        native_env = dict(env); native_env["PYTHONPATH"] = str(Path.home() / ".hermes" / "hermes-agent")
        python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        completed = subprocess.run([str(python), "-c", script], env=native_env, text=True, capture_output=True, timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert adapter.read_scoped_run(scope, task, run_id).get("status") not in {"running", "active"}
        for action in (lambda: ctl.register_planning_request(task), lambda: ctl.submit_plan(raw)):
            prior = counts(store)
            with pytest.raises((ValueError, ConflictError)): action()
            assert counts(store) == prior
        for version, cancel in ((1, False), (2, True)):
            store.set_operator_intent(PauseIntent(scope, "operator", version, True, cancel))
            for action in (lambda: ctl.register_planning_request(task), lambda: ctl.submit_plan(raw)):
                prior = counts(store)
                with pytest.raises((ValueError, ConflictError)): action()
                assert counts(store) == prior
    finally:
        store.close()
