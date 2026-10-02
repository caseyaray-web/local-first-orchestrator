"""Hermes plugin registration for the isolated Local First replacement.

Registration only installs CLI/tool/hook declarations.  It never opens the
plugin database, invokes Hermes, starts a loop, or mutates a board.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Mapping

# A wheel is not itself a native directory plugin.  The distribution therefore
# ships this documented directory-plugin wrapper with a private copy of its
# Python package at ``python/``.  Add only that artifact-local directory when
# it exists; a source checkout still imports through its normal package path.
_BUNDLED_PACKAGE_ROOT = Path(__file__).with_name("python")
if (_BUNDLED_PACKAGE_ROOT / "local_first_orchestrator").is_dir():
    sys.path.insert(0, str(_BUNDLED_PACKAGE_ROOT))

from local_first_orchestrator.composition import build_runtime
from local_first_orchestrator.composition import close_runtime
from local_first_orchestrator.config import PluginConfig
from local_first_orchestrator.cli import register_cli, run_command
from local_first_orchestrator.plugin_hooks import register_hooks
from local_first_orchestrator.plugin_tools import register_tools

__version__ = "0.1.0"


def runtime_factory(scope: Mapping[str, object]) -> Any:
    """Lazily compose only when an explicit CLI/tool invocation supplies scope."""
    raw_path = os.environ.get("LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    if type(raw_path) is not str or not raw_path:
        raise ValueError("LOCAL_FIRST_ORCHESTRATOR_CONFIG must name trusted plugin configuration")
    return build_runtime(PluginConfig.from_file(Path(raw_path)), scope=scope)


def member_role_from_worker_environment(task_id: str) -> str | None:
    """Read one configured scope only when a completion guard actually fires.

    Registration remains side-effect free.  The hook never guesses an anchor or
    queries Hermes internals: the configured board must equal the native worker
    environment, and membership comes from the public scoped evidence API.
    """
    if type(task_id) is not str or task_id != os.environ.get("HERMES_KANBAN_TASK"):
        return None
    board = os.environ.get("HERMES_KANBAN_BOARD")
    raw_path = os.environ.get("LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    if type(board) is not str or not board or type(raw_path) is not str or not raw_path:
        return None
    config = PluginConfig.from_file(Path(raw_path))
    if board != config.scope["board_id"]:
        return None
    runtime = build_runtime(config, scope=config.scope)
    try:
        members = runtime.store.read_scope(runtime.scope)["members"]
        matches = [member for member in members if member.task_id == task_id]
        return matches[0].role if len(matches) == 1 else None
    finally:
        close_runtime(runtime)


def register(ctx: object) -> None:
    """Register native surfaces without activating coordinator runtime state."""
    register_tools(ctx, runtime_factory=runtime_factory)  # type: ignore[arg-type]
    register_hooks(ctx, member_role=member_role_from_worker_environment)  # type: ignore[arg-type]
    register_native_cli = getattr(ctx, "register_cli_command", None)
    if not callable(register_native_cli):
        raise ValueError("Hermes plugin context lacks register_cli_command")
    register_native_cli(name="local-first-orchestrator", help="Operate the scoped Local First coordinator",
                        setup_fn=register_cli, handler_fn=run_command,
                        description="Scoped Local First coordinator controls with durable evidence and recovery.")


__all__ = ["__version__", "register", "runtime_factory", "member_role_from_worker_environment"]
