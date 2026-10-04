"""Focused M6 lifecycle regressions; all state is fixture-local."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from local_first_orchestrator.composition import build_runtime, close_runtime, initialize_store
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.contracts import ManagedMember
from local_first_orchestrator.plugin_tools import register_tools
from tests.test_operator_api import FixtureBoard, SCOPE, _config


class _Tools:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register_tool(self, *, name, handler, **_kwargs) -> None:
        self.handlers[name] = handler


def _config_json(config, path: Path) -> None:
    path.write_text(json.dumps({
        "version": 1, "state_root": str(config.state_root),
        "hermes_executable": str(config.hermes_executable), "hermes_home": str(config.hermes_home),
        "kanban_home": str(config.kanban_home),
        "trusted_roots": {key: str(value) for key, value in config.trusted_roots.items()},
        "roles": dict(config.roles),
        "budgets": {"implementation_attempts": 1, "review_corrections": 1,
                    "infrastructure_retries": 1, "workflow_repairs": 1, "paid_capacity": 1},
        "poll_interval_seconds": config.poll_interval_seconds, "scope": dict(config.scope),
        "check_commands": [{"check_id": check_id, "argv": list(argv)} for check_id, argv in config.check_commands],
    }), encoding="utf-8")


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("local_first_submit_plan", {"proposal_json": "{}"}),
    ("local_first_submit_review", {"task_id": "anchor", "candidate": {}, "review": {}}),
    ("local_first_request_corrections", {"task_id": "anchor", "candidate": {}, "review": {}, "operation_key": "fixture"}),
    ("local_first_report_issue", {"issue": {}}),
])
def test_m6_rejected_registered_worker_tools_close_runtime_before_a_later_valid_call(
    tmp_path: Path, monkeypatch, tool_name: str, arguments: dict[str, object]
) -> None:
    """Scope rejection closes its runtime; unbound capture rejects before composition."""
    import local_first_orchestrator.plugin_tools as plugin_tools

    config = _config(tmp_path)
    initialize_store(config)
    tools, runtimes, closed = _Tools(), [], []

    def factory(_requested_scope):
        board = FixtureBoard()
        board.list_tasks = lambda: tuple(board.cards.values())
        runtime = build_runtime(config, board=board, scope=SCOPE)
        runtimes.append(runtime)
        return runtime

    original_close = plugin_tools.close_runtime
    monkeypatch.setattr(plugin_tools, "close_runtime", lambda runtime: (closed.append(runtime), original_close(runtime))[1])
    monkeypatch.setenv("HERMES_M0_CLI", "")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "fixture-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "fixture-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "fixture-session")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", SCOPE["board_id"])
    register_tools(tools, runtime_factory=factory)

    mismatched_scope = json.loads(tools.handlers[tool_name]({**SCOPE, **arguments, "board_id": "other-board"}))
    assert mismatched_scope["ok"] is False
    assert "scope must match" in mismatched_scope["error"]

    monkeypatch.delenv("HERMES_SESSION_ID")
    unavailable_worker = json.loads(tools.handlers[tool_name]({**SCOPE, **arguments}))
    assert unavailable_worker == {"ok": False, "outcome": "invalid_or_held",
                                  "error": "native_worker_context_unbound",
                                  "missing_native_context_fields": ["session_id"]}

    valid = json.loads(tools.handlers["local_first_status"](dict(SCOPE)))
    assert valid["ok"] is True
    assert len(runtimes) == 2
    assert closed == runtimes
    with instance_lock(config.lock_path) as lock:
        lock.assert_held()


@pytest.mark.parametrize(("outcome", "exit_code"), [("partial", 4), ("held", 3), ("conflict", 5)])
def test_m6_cli_rendered_outcomes_close_fixture_runtime(tmp_path: Path, monkeypatch, capsys, outcome: str, exit_code: int) -> None:
    """Use cli.main with a production-built temporary Git/SQLite runtime."""
    import local_first_orchestrator.cli as cli

    config = _config(tmp_path)
    initialize_store(config)
    config_path = tmp_path / "plugin.json"
    _config_json(config, config_path)
    runtime = build_runtime(config, board=FixtureBoard(), scope=SCOPE)

    class Coordinator:
        def pause(self, *, stop=False):
            return {"outcome": outcome, "stop": stop}

    runtime.coordinator = Coordinator()
    closed = []
    original_close = cli.close_runtime
    monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: runtime)
    monkeypatch.setattr(cli, "close_runtime", lambda value: (closed.append(value), original_close(value))[1])
    assert cli.main(("--config", str(config_path), "--board", SCOPE["board_id"],
                     "--anchor-task-id", SCOPE["anchor_task_id"], "pause")) == exit_code
    assert json.loads(capsys.readouterr().out) == {"outcome": outcome, "stop": False}
    assert closed == [runtime]
    with instance_lock(config.lock_path) as lock:
        lock.assert_held()


def test_m6_cli_exception_closes_runtime_and_returns_invalid_json(tmp_path: Path, monkeypatch, capsys) -> None:
    import local_first_orchestrator.cli as cli

    config = _config(tmp_path)
    initialize_store(config)
    config_path = tmp_path / "plugin.json"
    _config_json(config, config_path)
    runtime = build_runtime(config, board=FixtureBoard(), scope=SCOPE)

    class Coordinator:
        def pause(self, *, stop=False):
            raise RuntimeError("fixture coordinator failure")

    runtime.coordinator = Coordinator()
    closed = []
    original_close = cli.close_runtime
    monkeypatch.setattr(cli, "build_runtime", lambda _config, *, scope: runtime)
    monkeypatch.setattr(cli, "close_runtime", lambda value: (closed.append(value), original_close(value))[1])
    assert cli.main(("--config", str(config_path), "--board", SCOPE["board_id"],
                     "--anchor-task-id", SCOPE["anchor_task_id"], "pause")) == 2
    assert json.loads(capsys.readouterr().out) == {"outcome": "invalid", "reason": "fixture coordinator failure"}
    assert closed == [runtime]
    with instance_lock(config.lock_path) as lock:
        lock.assert_held()


def test_m6_real_cli_loop_sigterm_restarts_with_persisted_pause(tmp_path: Path) -> None:
    """Run the actual CLI loop in bounded fixture subprocesses, never native Hermes."""
    config = _config(tmp_path)
    initialize_store(config)
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    store.register_member(ManagedMember("fixture-board", "anchor", "work", "implementation", 0, (), "fixture-work"))
    store.close()
    config_path = tmp_path / "plugin.json"
    _config_json(config, config_path)
    script = tmp_path / "fixture_cli.py"
    script.write_text(
        "from local_first_orchestrator.config import PluginConfig\n"
        "from local_first_orchestrator.composition import build_runtime\n"
        "from local_first_orchestrator.contracts import ActionResult, BoardSnapshot\n"
        "import local_first_orchestrator.cli as cli\n"
        "import signal, sys\n"
        "class Board:\n"
        "  is_fake=True\n"
        "  def __init__(self): self.create_lock_assertion=None; self.claim_create_attempt=None; self.cards={'anchor': BoardSnapshot({'id':'anchor','status':'blocked'},(),(),(),(),(),'fixture-anchor','anchor'), 'work': BoardSnapshot({'id':'work','status':'running'},(),({'id':'run-1','status':'running','stop_supported':False},),(),(),(),'fixture-work','work')}\n"
        "  def list_tasks(self): return tuple(self.cards.values())\n"
        "  def read_task(self, task_id): return self.cards[task_id]\n"
        "  def hold(self, action, task_id, reason): return ActionResult(action.key,'verified','held',self.cards[task_id].to_dict())\n"
        "  def release(self, action, task_id, reason): return ActionResult(action.key,'verified','released',self.cards[task_id].to_dict())\n"
        "  def stop_run(self, action, task_id, run_id, reason): return ActionResult(action.key,'unsupported','unsupported',self.cards[task_id].to_dict())\n"
        "config=PluginConfig.from_file(__import__('pathlib').Path(sys.argv[1]))\n"
        "cli.build_runtime=lambda _config, *, scope: build_runtime(config, board=Board(), scope=scope)\n"
        "code=cli.main(sys.argv[2:])\n"
        "print('RESTORED='+str(signal.getsignal(signal.SIGTERM) == signal.SIG_DFL), flush=True)\n"
        "raise SystemExit(code)\n", encoding="utf-8")
    command = (sys.executable, str(script), str(config_path), "--config", str(config_path), "--board", SCOPE["board_id"],
               "--anchor-task-id", SCOPE["anchor_task_id"])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1]), "HERMES_M0_CLI": ""}
    pause = subprocess.run((*command, "pause", "--stop"), text=True, capture_output=True, env=env, timeout=20)
    assert pause.returncode == 4, pause.stdout + pause.stderr

    loop = subprocess.Popen((*command, "run"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        time.sleep(0.35)
        loop.send_signal(signal.SIGTERM)
        stdout, stderr = loop.communicate(timeout=10)
    finally:
        if loop.poll() is None:
            loop.kill(); loop.communicate(timeout=5)
    assert loop.returncode == 0, stdout + stderr
    assert "RESTORED=True" in stdout
    assert json.loads(stdout.splitlines()[0])["outcome"] == "verified"
    with instance_lock(config.lock_path) as lock:
        lock.assert_held()

    restarted = subprocess.run((*command, "run", "--once"), text=True, capture_output=True, env=env, timeout=20)
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert "RESTORED=True" in restarted.stdout
    store = EvidenceStore.open(config.evidence_store_path, create_new=False)
    try:
        state = store.read_scope(SCOPE)
        assert state["operator_intent"] is not None and state["operator_intent"].active
        assert state["operations"]
    finally:
        store.close()
