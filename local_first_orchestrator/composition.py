"""Explicit runtime composition for the plugin-only coordinator.

Importing this module performs no configuration reads, database opens, provider
calls, board writes, or background-loop start.  Test adapters are accepted only
through an explicit argument; production always constructs HermesBoardAdapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .config import PluginConfig
from .coordinator import Coordinator
from .daemon import InstanceLock
from .evidence_store import EvidenceStore
from .hermes_board import HermesBoardAdapter


@dataclass(slots=True)
class Runtime:
    config: PluginConfig
    scope: Mapping[str, str]
    board: Any
    store: EvidenceStore
    coordinator: Coordinator


def _scope(scope: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) != {"board_id", "anchor_task_id"}:
        raise ValueError("explicit board_id and anchor_task_id scope is required")
    result = {}
    for key, value in scope.items():
        if type(value) is not str or not value or len(value) > 256:
            raise ValueError("scope values must be bounded non-empty strings")
        result[key] = value
    return result


def _run(config: PluginConfig, argv: tuple[str, ...], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run only configured commands with a hermetic non-interactive environment."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(config.hermes_home),
           "HERMES_HOME": str(config.hermes_home), "HERMES_M0_CLI": "", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(argv, cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, check=False)


def _git(config: PluginConfig, *args: str) -> str:
    result = _run(config, ("git", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", *args),
                  cwd=config.trusted_roots["repository"])
    if result.returncode != 0:
        raise ValueError("configured trusted repository Git observation failed")
    return result.stdout.strip()


def _combined_checks(config: PluginConfig, request: Mapping[str, Any]) -> Mapping[str, Any]:
    head = request.get("head_sha")
    if type(head) is not str or not head or _git(config, "rev-parse", "HEAD") != head:
        raise ValueError("combined checks require the exact current trusted integration head")
    checks = []
    for check_id, argv in config.check_commands:
        result = _run(config, argv, cwd=config.trusted_roots["repository"])
        checks.append({"check_id": check_id, "command": " ".join(argv), "exit_code": result.returncode,
                       "output_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()})
    return {"head_sha": head, "checks": checks}


def _git_observer(config: PluginConfig, scope: Mapping[str, str]) -> Mapping[str, Any]:
    if dict(scope) != dict(config.scope):
        raise ValueError("Git observer scope differs from configured scope")
    head = _git(config, "rev-parse", "HEAD")
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise ValueError("trusted repository HEAD is not a full Git SHA")
    status = _git(config, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ValueError("trusted repository is dirty; candidate observation is unavailable")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if type(run_id) is not str or not run_id:
        raise ValueError("Git candidate observation requires the native worker run")
    from .contracts import CandidateIdentity
    repository = str(config.trusted_roots["repository"])
    diff = hashlib.sha256(_git(config, "diff", "--no-ext-diff", f"{head}..{head}").encode()).hexdigest()
    candidate = CandidateIdentity(repository, str(config.trusted_roots["workspace"]), head, head,
                                  "git:" + hashlib.sha256((repository + head).encode()).hexdigest(),
                                  "diff:" + diff, run_id, "git:" + hashlib.sha256(head.encode()).hexdigest())
    checked = _combined_checks(config, {"head_sha": head})["checks"]
    checks = [{"check_id": item["check_id"], "outcome": "passed" if item["exit_code"] == 0 else "failed",
               "evidence": item["output_sha256"]} for item in checked]
    from .evidence_store import _canonical_identity
    return {"candidate": candidate.to_dict(), "checks": checks, "checks_identity": _canonical_identity(checks),
            "criterion_ids": [item["check_id"] for item in checks], "plan_id": None}


def build_runtime(config: PluginConfig, *, scope: Mapping[str, object], board: Any | None = None) -> Runtime:
    """Open a configured evidence store and assemble one scoped coordinator.

    The caller supplies a previously validated ``PluginConfig`` from a trusted
    local source.  A caller cannot select repository roots, profiles, budgets,
    executable, or database location through CLI/tool/browser request data.
    """
    if not isinstance(config, PluginConfig):
        raise ValueError("build_runtime requires validated PluginConfig")
    valid_scope = _scope(scope)
    if valid_scope != dict(config.scope):
        raise ValueError("runtime scope must match the configured board and anchor")
    store_path = config.evidence_store_path
    # Ordinary invocation is deliberately open-only.  A missing store is not a
    # harmless first-run condition: creating one here could erase the durable
    # budget/effect lineage an operator expected us to preserve.
    store = EvidenceStore.open(store_path, create_new=False)
    try:
        store._require_schema()
        if board is None:
            def member_lookup(candidate_scope: Mapping[str, str], task_id: str) -> bool:
                if dict(candidate_scope) != valid_scope or type(task_id) is not str:
                    return False
                return any(member.task_id == task_id for member in store.read_scope(valid_scope)["members"])

            board = HermesBoardAdapter(
                board=valid_scope["board_id"],
                anchor_task_id=valid_scope["anchor_task_id"],
                executable=str(config.hermes_executable),
                hermes_home=config.hermes_home,
                kanban_home=config.kanban_home,
                managed_member_lookup=member_lookup,
            )
        def planning_observer(candidate_scope: Mapping[str, str]) -> Mapping[str, Any]:
            if dict(candidate_scope) != valid_scope:
                raise ValueError("planning observer scope differs from configured scope")
            registration = store.read_planning_request(valid_scope)
            request = registration.get("request")
            if not isinstance(request, Mapping):
                raise ValueError("accepted planning request is unavailable")
            return {"request": dict(request)}
        coordinator = Coordinator(
            valid_scope,
            board=board,
            store=store,
            lock=InstanceLock(config.lock_path),
            budget_policy=config.budget_policy,
            configured_roles=config.roles,
            git_observer=lambda candidate_scope: _git_observer(config, candidate_scope),
            planning_observer=planning_observer,
            planning_profile=config.roles["planning_profile"],
            planning_workspace=str(config.trusted_roots["workspace"]),
            combined_check_runner=lambda request: _combined_checks(config, request),
        )
        return Runtime(config, valid_scope, board, store, coordinator)
    except BaseException:
        store.close()
        raise


def close_runtime(runtime: Runtime) -> None:
    """Close the plugin-owned evidence store; never alter native board state."""
    if not isinstance(runtime, Runtime):
        raise ValueError("close_runtime requires Runtime")
    runtime.store.close()


def initialize_store(config: PluginConfig) -> Path:
    """Explicit first-time store bootstrap; never called by ordinary runtime use."""
    if not isinstance(config, PluginConfig):
        raise ValueError("initialize_store requires validated PluginConfig")
    store = EvidenceStore.open(config.evidence_store_path, create_new=True)
    try:
        store.migrate()
    finally:
        store.close()
    return config.evidence_store_path


__all__ = ["Runtime", "build_runtime", "close_runtime", "initialize_store"]
