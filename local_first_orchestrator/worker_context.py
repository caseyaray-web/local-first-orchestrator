"""Trusted, immutable worker identity captured at a public plugin-tool entry."""
from __future__ import annotations

from dataclasses import dataclass
import os
import weakref


# This is deliberately module-private provenance, not a security boundary
# against arbitrary same-process Python.  It prevents ordinary public-tool
# callers from manufacturing a dataclass snapshot and presenting it as one
# captured at the trusted entry point.  The weak references make this bounded
# by live snapshots and bind provenance to the exact object, not a transferable
# attribute copied onto a lookalike snapshot.
_CAPTURED_CONTEXTS: dict[int, weakref.ReferenceType["NativeWorkerContext"]] = {}


class MissingNativeWorkerContextError(ValueError):
    """Value-free diagnostic for an unbound public worker-tool entry."""

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        self.missing_fields = missing_fields
        super().__init__("native_worker_context_unbound")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class NativeWorkerContext:
    """One worker-owned task/run/session/board tuple.

    The session component comes from Hermes' task-local session ContextVar bridge,
    not the process-global environment mirror.  This is a provisional live-worker
    observation only; native terminal metadata remains the authority for review
    finalization.
    """

    task_id: str
    run_id: str
    session_id: str
    board_id: str


def _remember_captured_context(context: NativeWorkerContext) -> None:
    """Register one exact live snapshot, removing only its own expired entry."""
    context_id = id(context)

    def cleanup(reference: weakref.ReferenceType[NativeWorkerContext]) -> None:
        if _CAPTURED_CONTEXTS.get(context_id) is reference:
            _CAPTURED_CONTEXTS.pop(context_id, None)

    _CAPTURED_CONTEXTS[context_id] = weakref.ref(context, cleanup)


def _is_captured_native_worker_context(context: object) -> bool:
    """Return whether this exact snapshot came from the trusted capture path."""
    if type(context) is not NativeWorkerContext:
        return False
    reference = _CAPTURED_CONTEXTS.get(id(context))
    return reference is not None and reference() is context


def capture_native_worker_context() -> NativeWorkerContext:
    """Capture exactly one supported Hermes worker context without logging values."""
    # Hermes v0.21.5's supported ContextVar bridge makes a task-local session
    # authoritative when the gateway has engaged it.  Task/run/board are native
    # worker lifecycle variables and have no corresponding session accessor.
    from gateway.session_context import get_session_env

    task_id = os.environ.get("HERMES_KANBAN_TASK")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    session_id = get_session_env("HERMES_SESSION_ID", "")
    board_id = os.environ.get("HERMES_KANBAN_BOARD")
    fields = (("task_id", task_id), ("run_id", run_id),
              ("session_id", session_id), ("board_id", board_id))
    missing_fields = tuple(name for name, value in fields
                           if type(value) is not str or not value)
    if missing_fields:
        # Deliberately field-only: callers and tool JSON must not disclose IDs,
        # session values, or any inherited environment contents.
        raise MissingNativeWorkerContextError(missing_fields)
    assert isinstance(task_id, str) and isinstance(run_id, str) and isinstance(session_id, str)
    assert isinstance(board_id, str)
    context = NativeWorkerContext(task_id, run_id, session_id, board_id)
    _remember_captured_context(context)
    return context
