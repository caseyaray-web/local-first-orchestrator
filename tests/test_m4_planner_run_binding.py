from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from local_first_orchestrator.decomposition_planner import serialize_proposal
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.planning_coordinator import evidence_payload
from tests.test_m4_plan_evidence import proposal
from tests.test_m4_planner_creation import native_fixture as _native_fixture, setup


@pytest.fixture
def native_fixture(tmp_path):
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to the pinned installed Hermes executable")
    return _native_fixture.__wrapped__(tmp_path)


def _claim(board, task, home):
    script = f'''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect(board={board!r}) as conn:
    claimed = kb.claim_task(conn, {task!r}, claimer="m4-paid-planner")
    assert claimed is not None and claimed.current_run_id is not None, claimed
    print(claimed.current_run_id)
'''
    python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    native_env = dict(os.environ)
    native_env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_KANBAN_BOARD=board)
    native_env["PYTHONPATH"] = str(Path.home() / ".hermes" / "hermes-agent")
    result = subprocess.run([str(python), "-c", script], env=native_env, text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_applied_paid_release_binds_native_run_without_a_second_charge(tmp_path, native_fixture, monkeypatch):
    board, anchor, _workspace, adapter, membership, cli = native_fixture
    controller, store, scope, req = setup(tmp_path, native_fixture)
    try:
        task = controller.prepare_planner(request_id="operator-alias")["task_id"]
        released = controller.release_planner(request_id="operator-alias")
        assert released["outcome"] == "released"
        assert len([event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]) == 1
        run_id = _claim(board, task, tmp_path / "home")
        session = "controlled-m4-session"
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
        monkeypatch.setenv("HERMES_SESSION_ID", session)

        registration = controller.register_planning_request(task)
        assert registration["planner_task_id"] == task
        raw = serialize_proposal(proposal(req))
        evidence = controller.submit_plan(raw)
        binding = store.read_paid_release_run_binding(scope, released["operation_key"])
        assert (binding["task_id"], binding["run_id"], binding["session_id"], binding["profile"]) == (
            task, run_id, session, "planner")
        assert store.read_plan(scope, proposal(req).plan.plan_id) == evidence
        assert len([event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]) == 1

        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        membership["store"] = store
        controller.store = store
        assert controller.register_planning_request(task) == registration
        assert controller.submit_plan(raw) == evidence
        assert store.read_paid_release_run_binding(scope, released["operation_key"]) == binding
        assert len([event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]) == 1
    finally:
        store.close()


def test_managed_planner_without_release_proof_fails_closed_before_accounting(tmp_path, native_fixture, monkeypatch):
    _board, anchor, _workspace, adapter, membership, _cli = native_fixture
    controller, store, scope, req = setup(tmp_path, native_fixture)
    try:
        task = controller.prepare_planner(request_id="missing-release")["task_id"]
        # The task remains held; neither a missing proof nor a forged run observation
        # may create a paid charge, binding, registration, or plan.
        monkeypatch.setenv("HERMES_KANBAN_TASK", task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "forged-run")
        monkeypatch.setenv("HERMES_SESSION_ID", "session")
        before = store.read_scope(scope)
        with pytest.raises((ValueError, KeyError), match="running native task|Hermes run not found"):
            controller.register_planning_request(task)
        after = store.read_scope(scope)
        assert after["budget_events"] == before["budget_events"]
        assert after["operations"] == before["operations"]
        with pytest.raises(KeyError):
            store.read_paid_release_run_binding(scope, "absent-release")
    finally:
        store.close()
