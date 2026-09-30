from __future__ import annotations

import dataclasses
import subprocess

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import ConflictError, PauseIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import request_payload
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_m4_planner_creation import native_fixture as _creation_fixture, setup


@pytest.fixture
def native_fixture(tmp_path):
    import os
    from pathlib import Path
    executable = os.environ.get("HERMES_M0_CLI")
    assert executable and Path(executable).is_file(), "native release tests require HERMES_M0_CLI"
    return _creation_fixture.__wrapped__(tmp_path)


def test_release_planner_reserves_before_exact_native_unblock(tmp_path, native_fixture):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    try:
        held = controller.prepare_planner(request_id="planning-request-1")
        task_id = held["task_id"]
        adapter = controller.board
        real_runner = adapter.runner
        calls = []

        def runner(argv, **kwargs):
            if "unblock" in argv:
                assert len([event for event in store.read_scope(scope)["budget_events"]
                            if event["event_id"].startswith("paid_capacity:")]) == 1
                calls.append(tuple(argv))
            return real_runner(argv, **kwargs)

        adapter.runner = runner
        result = controller.release_planner(request_id="planning-request-1")
        assert result["outcome"] == "released"
        assert result["task_id"] == task_id
        assert len(calls) == 1
        snapshot = adapter.read_task(task_id)
        assert snapshot.native_task["status"] in {"ready", "todo"}
        operation = next(op for op in store.read_scope(scope)["operations"] if op.effect == "release")
        assert operation.phase == "applied"
        assert not snapshot.runs
        assert not [event for event in store.read_scope(scope)["budget_events"]
                    if event.get("source_kind") == "native_run"]
        assert controller.release_planner(request_id="planning-request-1")["actions_attempted"] == 0
        assert len(calls) == 1
        assert len([event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]) == 1
    finally:
        store.close()


def test_lost_unblock_response_is_unknown_then_readonly_marker_reconciles_after_restart(tmp_path, native_fixture):
    controller, store, scope, req = setup(tmp_path, native_fixture)
    adapter = controller.board
    real_runner = adapter.runner
    unblock_calls = []
    fail_response = {"once": True}

    def runner(argv, **kwargs):
        if "unblock" in argv:
            paid = [event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]
            operation = next(op for op in store.read_scope(scope)["operations"] if op.effect == "release")
            assert len(paid) == 1
            assert operation.phase == "unknown" and operation.outcome == "ambiguous"
            unblock_calls.append(tuple(argv))
            completed = real_runner(argv, **kwargs)
            if fail_response["once"]:
                fail_response["once"] = False
                return subprocess.CompletedProcess(argv, 1, "", "simulated lost unblock response")
            return completed
        return real_runner(argv, **kwargs)

    adapter.runner = runner
    task_id = controller.prepare_planner(request_id="release-request")["task_id"]
    try:
        partial = controller.release_planner(request_id="release-request")
        assert partial["outcome"] == "partial" and partial["actions_attempted"] == 1
        state = store.read_scope(scope)
        operation = next(op for op in state["operations"] if op.effect == "release")
        assert operation.phase == "unknown" and operation.outcome == "ambiguous"
        assert len([e for e in state["budget_events"] if e["event_id"].startswith("paid_capacity:")]) == 1
        assert adapter.read_task(task_id).native_task["status"] in {"ready", "todo"}
        store.close()

        board, anchor, workspace, adapter, membership, _cli = native_fixture
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        req = dataclasses.replace(req, board_id=board, anchor_id=anchor)
        controller = Coordinator(scope, board=adapter, store=store,
            lock=instance_lock(tmp_path / "coordinator.lock"), budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
            configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner"},
            planning_observer=lambda _scope: {"request": request_payload(req)}, planning_profile="planner",
            planning_workspace=workspace)
        membership["store"] = store
        def guarded_runner(argv, **kwargs):
            assert "unblock" not in argv, "unknown release must never blindly replay"
            return real_runner(argv, **kwargs)
        adapter.runner = guarded_runner
        retried = controller.release_planner(request_id="release-request")
        assert retried == {"outcome": "released", "task_id": task_id,
                           "operation_key": partial["operation_key"], "actions_attempted": 0}
        state = store.read_scope(scope)
        operation = next(op for op in state["operations"] if op.effect == "release")
        assert operation.phase == "applied" and operation.outcome in {"verified", "no-op"}
        assert len([e for e in state["budget_events"] if e["event_id"].startswith("paid_capacity:")]) == 1
        snapshot = adapter.read_task(task_id)
        assert snapshot.native_task["status"] in {"ready", "todo"}
        assert any(item.get("body") == f"UNBLOCK: {adapter._native_marker(controller._action_from_intent(operation))}"
                   for item in snapshot.comments)
        assert not snapshot.runs
        assert not [e for e in state["budget_events"] if e.get("source_kind") == "native_run"]
        assert len(unblock_calls) == 1
    finally:
        store.close()


@pytest.mark.parametrize("creation_alias,release_alias", [
    ("original-alias", "different-alias"), (None, "explicit-after-implicit"),
    ("explicit-alias", None),
])
def test_release_alias_is_persisted_creation_proof_not_request_identity(tmp_path, native_fixture, creation_alias, release_alias):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    try:
        controller.prepare_planner(request_id=creation_alias)
        before = store.read_scope(scope)
        operation_count = len(before["operations"])
        budget_events = tuple(before["budget_events"])
        task_id = next(member.task_id for member in before["members"] if member.role == "planner")
        runner = controller.board.runner
        def guarded_runner(argv, **kwargs):
            assert "unblock" not in argv
            return runner(argv, **kwargs)
        controller.board.runner = guarded_runner
        with pytest.raises((ValueError, ConflictError)):
            controller.release_planner(request_id=release_alias)
        after = store.read_scope(scope)
        assert len(after["operations"]) == operation_count
        assert after["budget_events"] == budget_events
        assert controller.board.read_task(task_id).native_task["status"] == "blocked"
        assert not [op for op in after["operations"] if op.effect == "release"]
        assert not [event for event in after["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]
    finally:
        store.close()


@pytest.mark.parametrize("drift", ["profile", "workspace", "request"])
def test_release_refuses_trusted_context_drift_without_new_charge(tmp_path, native_fixture, drift):
    observed = {}
    controller, store, scope, req = setup(tmp_path, native_fixture, observed=observed)
    try:
        observed["request"] = request_payload(req)
        controller.prepare_planner(request_id="stable-alias")
        if drift == "profile":
            controller.planning_profile = "other-planner"
        elif drift == "workspace":
            controller.planning_workspace = str(tmp_path.parent)
        else:
            observed["request"] = request_payload(dataclasses.replace(req, base_sha="f" * 40))
        with pytest.raises((ValueError, ConflictError)):
            controller.release_planner(request_id="stable-alias")
        assert not [op for op in store.read_scope(scope)["operations"] if op.effect == "release"]
        assert not [event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]
    finally:
        store.close()


def test_pause_or_zero_paid_capacity_refuses_before_new_release_reservation(tmp_path, native_fixture):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    try:
        held_id = controller.prepare_planner(request_id="pause-before-release")["task_id"]
        store.set_operator_intent(PauseIntent(scope, "operator", 1, True, False))
        with pytest.raises(ValueError, match="pause"):
            controller.release_planner(request_id="pause-before-release")
        events = [event for event in store.read_scope(scope)["budget_events"]
                  if event["event_id"].startswith("paid_capacity:")]
        assert not events
        assert not [op for op in store.read_scope(scope)["operations"] if op.effect == "release"]
        assert controller.board.read_task(held_id).native_task["status"] == "blocked"
    finally:
        store.close()


def test_zero_paid_capacity_and_cancel_refuse_before_release_reservation(tmp_path, native_fixture):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    try:
        task_id = controller.prepare_planner(request_id="no-capacity")["task_id"]
        controller.budget_policy = BudgetPolicy(2, 2, 2, 2, 0)
        with pytest.raises(ConflictError):
            controller.release_planner(request_id="no-capacity")
        assert not [event for event in store.read_scope(scope)["budget_events"]
                    if event["event_id"].startswith("paid_capacity:")]
        store.set_operator_intent(PauseIntent(scope, "operator", 1, True, True))
        with pytest.raises(ValueError, match="pause"):
            controller.release_planner(request_id="no-capacity")
        assert not [op for op in store.read_scope(scope)["operations"] if op.effect == "release"]
        assert controller.board.read_task(task_id).native_task["status"] == "blocked"
    finally:
        store.close()


def test_applied_release_retry_rejects_native_comment_drift_and_alias_change(tmp_path, native_fixture):
    controller, store, scope, _req = setup(tmp_path, native_fixture)
    try:
        task_id = controller.prepare_planner(request_id="release-alias")["task_id"]
        released = controller.release_planner(request_id="release-alias")
        assert released["outcome"] == "released" and released["actions_attempted"] == 1
        charge_before = [event for event in store.read_scope(scope)["budget_events"]
                         if event["event_id"].startswith("paid_capacity:")]
        assert len(charge_before) == 1
        with pytest.raises((ValueError, ConflictError)):
            controller.release_planner(request_id="new-alias")
        native_fixture[5]("comment", task_id, "supported native operator comment")
        with pytest.raises(ValueError, match="applied release readback"):
            controller.release_planner(request_id="release-alias")
        assert [event for event in store.read_scope(scope)["budget_events"]
                if event["event_id"].startswith("paid_capacity:")] == charge_before
        assert len([op for op in store.read_scope(scope)["operations"] if op.effect == "release"]) == 1
    finally:
        store.close()
