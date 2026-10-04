from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.contracts import BoardSnapshot


SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}


def _adapter(tmp_path: Path, run: dict) -> HermesBoardAdapter:
    executable = tmp_path / "hermes"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    home = tmp_path / "home"
    (home / "profiles" / "implementer").mkdir(parents=True)
    adapter = HermesBoardAdapter(
        board="fixture-board", anchor_task_id="anchor", executable=str(executable),
        hermes_home=home, kanban_home=home,
    )
    adapter.read_scoped_run = lambda scope, task_id, run_id: dict(run)  # type: ignore[method-assign]
    return adapter


def _session_db(adapter: HermesBoardAdapter) -> Path:
    path = adapter.hermes_home / "profiles" / "implementer" / "state.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT, started_at REAL, ended_at REAL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            ("live-session", "kanban", "implementer", 101.0, None),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_active_worker_identity_accepts_exact_native_session_metadata(tmp_path):
    adapter = _adapter(tmp_path, {
        "id": "7", "task_id": "implementation", "profile": "implementer",
        "status": "running", "metadata": {"worker_session_id": "live-session"},
    })

    receipt = adapter.read_active_worker_identity(SCOPE, "implementation", "7", "live-session", "implementer")

    assert receipt == {
        "version": 1, "task_id": "implementation", "run_id": "7",
        "session_id": "live-session", "profile": "implementer",
        "source": "native_run_metadata",
    }


@pytest.mark.parametrize(("task_id", "run_id"), [
    ("sibling-parallel-task", "7"),
    ("implementation", "older-parallel-run"),
])
def test_active_worker_identity_rejects_sibling_or_older_task_run(tmp_path, task_id, run_id):
    adapter = _adapter(tmp_path, {
        "id": "7", "task_id": "implementation", "profile": "implementer",
        "status": "running", "metadata": {"worker_session_id": "live-session"},
    })

    def exact_run(scope, actual_task, actual_run):
        if actual_task != "implementation" or actual_run != "7":
            raise ValueError("native_run_not_found")
        return {"id": "7", "task_id": "implementation", "profile": "implementer",
                "status": "running", "metadata": {"worker_session_id": "live-session"}}

    adapter.read_scoped_run = exact_run  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="native_run_not_found"):
        adapter.read_active_worker_identity(SCOPE, task_id, run_id, "live-session", "implementer")


def test_active_null_metadata_fails_closed_without_profile_database_access_or_mutation(tmp_path):
    adapter = _adapter(tmp_path, {
        "id": "7", "task_id": "implementation", "profile": "implementer",
        "status": "running", "metadata": None,
    })
    state_db = _session_db(adapter)
    before = state_db.read_bytes()

    with pytest.raises(ValueError, match="native_session_unbound"):
        adapter.read_active_worker_identity(SCOPE, "implementation", "7", "live-session", "implementer")

    assert state_db.read_bytes() == before


def test_provisional_context_accepts_real_active_null_metadata_without_session_authority(tmp_path):
    run = {"id": "7", "task_id": "implementation", "profile": "implementer",
           "status": "running", "metadata": None}
    adapter = _adapter(tmp_path, run)
    adapter.read_task = lambda _task_id: BoardSnapshot(
        {"id": "implementation", "status": "running", "assignee": "implementer"},
        (), (run,), (), (), (), "fixture-board", "implementation")  # type: ignore[method-assign]

    receipt = adapter.read_provisional_worker_context(
        SCOPE, "implementation", "7", "live-session", "implementer")

    assert receipt == {"version": 1, "task_id": "implementation", "run_id": "7",
                       "session_id": "live-session", "profile": "implementer",
                       "source": "active_worker_context"}
