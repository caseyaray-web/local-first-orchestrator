"""Explicit runtime composition for the plugin-only coordinator.

Importing this module performs no configuration reads, database opens, provider
calls, board writes, or background-loop start.  Test adapters are accepted only
through an explicit argument; production always constructs HermesBoardAdapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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


def operator_planning_request(config: PluginConfig, request_file: Path) -> Any:
    """Build one immutable initial request from operator text and trusted facts."""
    if not isinstance(config, PluginConfig) or not isinstance(request_file, Path):
        raise ValueError("validated configuration and request file are required")
    if not request_file.is_absolute() or request_file.is_symlink() or not request_file.is_file():
        raise ValueError("request file must be an absolute regular JSON file")
    try:
        raw = json.loads(request_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("request file must contain valid JSON") from error
    if not isinstance(raw, Mapping) or set(raw) != {"version", "objective", "non_goals", "criteria", "authorized_paths"} or raw["version"] != 1:
        raise ValueError("request file must contain exactly version, objective, non_goals, criteria, and authorized_paths")
    objective, non_goals, criteria, authorized_paths = raw["objective"], raw["non_goals"], raw["criteria"], raw["authorized_paths"]
    if type(objective) is not str or type(non_goals) is not list or any(type(item) is not str for item in non_goals):
        raise ValueError("operator objective and non_goals must be strings")
    if not isinstance(criteria, list) or not criteria or any(not isinstance(item, Mapping) or set(item) != {"id", "statement"} or type(item["id"]) is not str or type(item["statement"]) is not str for item in criteria):
        raise ValueError("criteria must be a non-empty list of id/statement objects")
    if len({item["id"] for item in criteria}) != len(criteria):
        raise ValueError("criteria IDs must be unique")
    if (not isinstance(authorized_paths, list) or not authorized_paths
            or any(type(path) is not str or not path or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")) for path in authorized_paths)
            or len(set(authorized_paths)) != len(authorized_paths)):
        raise ValueError("authorized_paths must be unique safe repository-relative paths")
    head = _git(config, "rev-parse", "HEAD")
    if _git(config, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("trusted repository is dirty; planning bootstrap is unavailable")
    paths = tuple(line for line in _git(config, "ls-files", "-z").split("\0") if line)
    if not paths:
        raise ValueError("trusted repository has no tracked planning paths")
    # Pin every tree observation to the full SHA captured before status.  A
    # final HEAD read rejects a repository that moved while the request was
    # assembled instead of persisting a mixed-revision identity.
    snapshot = hashlib.sha256(_git(config, "ls-tree", "-r", head).encode("utf-8")).hexdigest()
    if _git(config, "rev-parse", "HEAD") != head:
        raise ValueError("trusted repository HEAD changed during planning bootstrap observation")
    contract = {"objective": objective, "non_goals": non_goals, "criteria": criteria,
                "repository": str(config.trusted_roots["repository"]), "head": head,
                "authorized_paths": sorted(authorized_paths), "checks": [(key, list(argv)) for key, argv in config.check_commands]}
    from .decomposition_planner import PlanningRequest
    return PlanningRequest(
        board_id=config.scope["board_id"], anchor_id=config.scope["anchor_task_id"],
        repository_identity=str(config.trusted_roots["repository"]), base_sha=head,
        snapshot_hash=snapshot, root_contract_hash=hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
        expected_criteria=frozenset(item["id"] for item in criteria), authorized_paths=frozenset(authorized_paths),
        max_tranches=8, max_tickets=64, max_context_tokens=65536, max_patch_files=128,
        max_patch_lines=20000, max_attempts=16,
        verification_commands=tuple(argv for _key, argv in config.check_commands),
        verification_timeout_seconds=300, verification_output_limit=1_000_000,
        max_payload_bytes=1_000_000, max_json_depth=32, objective=objective,
        non_goals=tuple(non_goals), criterion_statements=tuple((item["id"], item["statement"]) for item in criteria),
    )


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
            bootstrap = store.read_bootstrap_planning_request(valid_scope)
            request = bootstrap.get("request")
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


def build_git_adapter(config: PluginConfig) -> Any:
    """Construct the only Git adapter available to public lifecycle handlers.

    Roots come exclusively from the already validated bootstrap configuration;
    command and tool arguments never select a repository or worktree root.
    """
    if not isinstance(config, PluginConfig):
        raise ValueError("build_git_adapter requires validated PluginConfig")
    from .git_adapter import GitWorktreeAdapter
    return GitWorktreeAdapter(config.trusted_roots["repository"], config.trusted_roots["workspace"])


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


__all__ = ["Runtime", "build_runtime", "build_git_adapter", "close_runtime", "initialize_store", "operator_planning_request"]
