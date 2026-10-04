"""M7 regression: public worker context is one immutable Hermes-context snapshot."""
from __future__ import annotations

import copy
import gc
import json
from types import SimpleNamespace
import weakref

import pytest

from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.plugin_tools import _runtime_for_args, register_tools
from local_first_orchestrator import worker_context
from local_first_orchestrator.worker_context import NativeWorkerContext, capture_native_worker_context


SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}


def test_capture_uses_hermes_contextvar_session_when_process_global_is_different(monkeypatch, scoped_current_session_id):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-session")

    with scoped_current_session_id("current-worker-session"):
        context = capture_native_worker_context()

    assert context == NativeWorkerContext("implementation", "7", "current-worker-session", "fixture-board")


def test_public_entry_snapshot_survives_mid_operation_global_environment_change(monkeypatch, scoped_current_session_id):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-session")
    bound: list[NativeWorkerContext] = []

    def factory(_scope):
        # A later global mutation must not change the already captured tuple.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "sibling")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "other-run")
        monkeypatch.setenv("HERMES_SESSION_ID", "other-process-session")
        return SimpleNamespace(scope=SCOPE, coordinator=SimpleNamespace(
            _bind_native_worker_context=lambda context, **_kwargs: bound.append(context)))

    with scoped_current_session_id("current-worker-session"):
        runtime = _runtime_for_args(factory, SCOPE, require_worker=True)

    assert runtime.scope == SCOPE
    assert bound == [NativeWorkerContext("implementation", "7", "current-worker-session", "fixture-board")]


def test_coordinator_rejects_misbound_task_or_run_without_echoing_identity_values():
    coordinator = Coordinator.__new__(Coordinator)
    coordinator._native_worker_context = NativeWorkerContext("implementation", "7", "session", "fixture-board")

    for kwargs in ({"task_id": "sibling"}, {"run_id": "old-run"}):
        with pytest.raises(ValueError, match="^native_worker_context_task_run_mismatch$") as error:
            coordinator._current_native_worker_context(**kwargs)
        assert "implementation" not in str(error.value)
        assert "session" not in str(error.value)


def test_coordinator_rejects_a_caller_constructed_snapshot_but_reuses_captured_context(monkeypatch, scoped_current_session_id):
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.scope = SCOPE
    coordinator._native_worker_context = None
    forged = NativeWorkerContext("implementation", "7", "session", "fixture-board")

    with pytest.raises(ValueError, match="^native_worker_context_unbound$"):
        coordinator._bind_native_worker_context(forged)

    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    with scoped_current_session_id("captured-session"):
        captured = capture_native_worker_context()
    coordinator._bind_native_worker_context(captured)

    # A later process-global change cannot replace the public-entry snapshot
    # consumed by coordinator observers.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "sibling")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "other-run")
    assert coordinator._ensure_native_worker_context() is captured
    assert coordinator._current_native_worker_context(task_id="implementation", run_id="7") is captured


def test_coordinator_rejects_shallow_copy_of_captured_snapshot(monkeypatch, scoped_current_session_id):
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.scope = SCOPE
    coordinator._native_worker_context = None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    with scoped_current_session_id("captured-session"):
        captured = capture_native_worker_context()
    copied = copy.copy(captured)

    assert copied is not captured
    with pytest.raises(ValueError, match="^native_worker_context_unbound$"):
        coordinator._bind_native_worker_context(copied)
    coordinator._bind_native_worker_context(captured)


def test_captured_context_identity_entry_is_removed_when_the_context_is_collected(monkeypatch, scoped_current_session_id):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    with scoped_current_session_id("captured-session"):
        captured = capture_native_worker_context()
    context_id = id(captured)
    reference = weakref.ref(captured)

    assert context_id in worker_context._CAPTURED_CONTEXTS
    del captured
    gc.collect()

    assert reference() is None
    assert context_id not in worker_context._CAPTURED_CONTEXTS


def test_coordinator_rejects_captured_snapshot_for_another_board(monkeypatch, scoped_current_session_id):
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.scope = SCOPE
    coordinator._native_worker_context = None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "foreign-board")

    with scoped_current_session_id("session"):
        context = capture_native_worker_context()
    with pytest.raises(ValueError, match="^native_worker_context_board_mismatch$"):
        coordinator._bind_native_worker_context(context)


def test_capture_fails_field_only_when_contextual_session_is_blank(monkeypatch, scoped_current_session_id):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "implementation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fixture-board")
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-session")

    with scoped_current_session_id(""):
        with pytest.raises(ValueError, match="^native_worker_context_unbound$") as error:
            capture_native_worker_context()
    assert "foreign-process-session" not in str(error.value)


@pytest.mark.parametrize("missing_field", ("task_id", "run_id", "session_id", "board_id"))
def test_registered_request_local_review_reports_each_missing_field_before_runtime_or_effects(
        monkeypatch, scoped_current_session_id, missing_field):
    """The installed public handler diagnoses an unbound worker without composing.

    The sentinels cover every mutable stage a successful request could reach:
    composition, store access, candidate/check construction, intent reservation,
    and native calls.  A missing capture field must leave all of them untouched.
    """
    names = {"task_id": "HERMES_KANBAN_TASK", "run_id": "HERMES_KANBAN_RUN_ID",
             "board_id": "HERMES_KANBAN_BOARD"}
    for name in names.values():
        monkeypatch.setenv(name, "trusted-" + name.lower())
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-process-session")
    if missing_field == "session_id":
        session = ""
    else:
        monkeypatch.delenv(names[missing_field], raising=False)
        session = "trusted-context-session"

    registered, effects = {}, []

    class Context:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs

    def forbidden_runtime_factory(_scope):
        effects.append("runtime_composed")
        raise AssertionError("missing worker context must fail before composition")

    register_tools(Context(), runtime_factory=forbidden_runtime_factory)
    with scoped_current_session_id(session):
        response = json.loads(registered["local_first_request_local_review"]["handler"](
            {"board_id": "fixture-board", "anchor_task_id": "anchor",
             "operation_key": "diagnostic-only", "summary": "context diagnosis"}))

    assert response == {"ok": False, "outcome": "invalid_or_held",
                        "error": "native_worker_context_unbound",
                        "missing_native_context_fields": [missing_field]}
    assert effects == []
