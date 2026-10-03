"""Scoped dashboard API backed only by the plugin composition root.

The host mounts ``router`` below ``/api/plugins/local-first-orchestrator``.
Browser payloads can select a named operator action, but never a repository,
executable, database, profile, budget, board, or anchor.  Those values come
from the trusted local PluginConfig and trusted bootstrap scope.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from local_first_orchestrator.composition import Runtime, build_runtime, close_runtime
from local_first_orchestrator.config import PluginConfig

_PREFIX = "/api/plugins/local-first-orchestrator"
RuntimeFactory = Callable[[], Runtime]
_runtime_factory: RuntimeFactory | None = None
_configuration_path_for_tests: Path | None = None


def _plain(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return "<depth-limited>"
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(child, depth=depth + 1) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(child, depth=depth + 1) for child in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict(), depth=depth + 1)
    if is_dataclass(value):
        return _plain({name: getattr(value, name) for name in value.__dataclass_fields__}, depth=depth + 1)
    raise ValueError("dashboard result contains unsupported record")


def _trusted_config_path() -> Path:
    raw_config = os.environ.get("LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    if not raw_config:
        raise ValueError("dashboard requires trusted LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    path = Path(raw_config)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("dashboard configuration bootstrap must be an absolute regular file")
    return path


def _trusted_runtime() -> Runtime:
    """Compose from server-local bootstrap only; request data is never consulted."""
    config = PluginConfig.from_file(_trusted_config_path())
    return build_runtime(config, scope=config.scope)


def configure_runtime_factory(factory: RuntimeFactory | None, *, configuration_path: Path | None = None) -> None:
    """Test-only injection point; production leaves this unset.

    A test may supply an already-created absolute bootstrap file to exercise
    persistence. It remains server setup, never request data.
    """
    global _runtime_factory, _configuration_path_for_tests
    if configuration_path is not None and (not configuration_path.is_absolute() or not configuration_path.is_file()):
        raise ValueError("test configuration path must be an absolute regular file")
    _runtime_factory = factory
    _configuration_path_for_tests = configuration_path


def _open_runtime() -> Runtime:
    return _trusted_runtime() if _runtime_factory is None else _runtime_factory()


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _configuration(config: PluginConfig) -> dict[str, Any]:
    """The only configuration projection a browser may read or propose edits to."""
    return {
        "poll_interval_seconds": config.poll_interval_seconds,
        "budgets": asdict(config.budget_policy),
        "roles": dict(config.roles),
        "scope": dict(config.scope),
    }


def _configuration_with_digest(config: PluginConfig) -> dict[str, Any]:
    projection = _configuration(config)
    projection["configuration_digest"] = _digest(projection)
    return projection


def _runtime_metrics(observed: Mapping[str, Any], native_runs: list[Mapping[str, Any]],
                     operations: list[Mapping[str, Any]], reviews: list[Mapping[str, Any]],
                     budget_events: list[Mapping[str, Any]], budget_net: list[Mapping[str, Any]],
                     configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Project only evidence already read by ``Coordinator.status``.

    The mounted dashboard opens a short-lived observer for each request, so it
    cannot truthfully report a coordinator-loop heartbeat or process liveness.
    Native run lanes are observations, not proof that a worker PID is alive.
    """
    active_states = {"active", "claimed", "running", "stopping"}
    limits = configuration["budgets"]
    by_finding: list[dict[str, Any]] = []
    for item in budget_net:
        root, finding, category, net = (item.get("root_task_id"), item.get("finding_id"),
                                        item.get("category"), item.get("net"))
        if (isinstance(root, str) and isinstance(finding, str) and category in limits
                and type(net) is int):
            limit = limits[category]
            by_finding.append({"root_task_id": root, "finding_id": finding, "category": category,
                               "consumed_net": net, "configured_limit": limit,
                               "remaining": limit - net})
    return {
        "loop": {
            "state": "unknown",
            "reason": "no persistent coordinator-loop heartbeat is stored in the evidence scope",
            "last_heartbeat": None,
        },
        "native_runs": {
            "observed_total": len(native_runs),
            "observed_active": sum(1 for run in native_runs if run.get("status") in active_states),
            "liveness": "unknown",
            "source": "current native board observation",
        },
        "operations": {
            "total": len(operations),
            "applied": sum(1 for item in operations if item.get("phase") == "applied"),
            "pending": sum(1 for item in operations if item.get("phase") == "pending"),
            "unknown": sum(1 for item in operations if item.get("phase") == "unknown"),
        },
        "reviews": {
            "total": len(reviews),
            "local": sum(1 for item in reviews if item.get("reviewer_role") == "local"),
            "paid": sum(1 for item in reviews if item.get("reviewer_role") == "paid"),
        },
        "budget_usage": {
            "by_finding": by_finding,
            "aggregate": {
                "consumed_net": sum(item["consumed_net"] for item in by_finding),
                "semantics": "sum across recorded finding/category ledger rows; not an enforcement ceiling",
            },
        },
        "budget_events_recorded": len(budget_events),
        "observation_source": "current scoped evidence store and native board readback",
    }


def _status(runtime: Runtime, *, configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
    observed = _plain(runtime.coordinator.status())
    members = observed.get("members", [])
    native_tasks = observed.get("native_tasks", {})
    native_runs = observed.get("native_runs", [])
    operations = observed.get("operations", [])
    reviews = observed.get("reviews", [])
    budget_events = observed.get("budget_events", [])
    budget_net = observed.get("budget_net", [])
    intent = observed.get("operator_intent")
    active_workers = [run for run in native_runs if run.get("status") in {"active", "claimed", "running", "stopping"}]
    preserved_paths = sorted({str(value) for item in operations for value in (
        item.get("target", {}).get("worktree"), item.get("target", {}).get("workspace"),
        item.get("readback", {}).get("worktree") if isinstance(item.get("readback"), dict) else None,
    ) if isinstance(value, str) and value})
    configuration = _configuration_with_digest(runtime.config) if configuration is None else dict(configuration)
    projection = {
        "scope": observed.get("scope", dict(runtime.scope)),
        "anchor": native_tasks.get(runtime.scope["anchor_task_id"]),
        "managed_anchors": members,
        "current_head": observed.get("git_observation"),
        "reviews": reviews,
        "review_queues": {
            "local": [item for item in reviews if item.get("reviewer_role") == "local"],
            "paid": [item for item in reviews if item.get("reviewer_role") == "paid"],
        },
        "repair_history": operations[-50:],
        "budgets": budget_events,
        "operator_intent": intent,
        "active_workers": active_workers,
        "uncontained_workers": active_workers if intent and intent.get("active") else [],
        "preserved_paths": preserved_paths,
        "pending_operations": [item for item in operations if item.get("phase") in {"pending", "unknown"}],
        "configuration": configuration,
        "runtime_metrics": _runtime_metrics(observed, native_runs, operations, reviews, budget_events, budget_net, configuration),
    }
    projection["observation_digest"] = _digest(projection)
    return projection


def _error(status: int, detail: str, *, fresh: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {"detail": detail}
    if fresh is not None:
        body["status"] = fresh
    return status, body


def _expected_digest(payload: Mapping[str, Any]) -> str:
    allowed = {"expected_observation_digest", "authorized_clear"}
    if set(payload) - allowed:
        raise ValueError("dashboard action payload contains unsupported fields")
    digest = payload.get("expected_observation_digest")
    if type(digest) is not str or not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("expected_observation_digest is required")
    return digest


def _update_configuration(runtime: Runtime, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only tightening edits to the server-selected bootstrap file.

    The browser never supplies a path, executable, root, board, anchor, or
    profile.  Increasing a finite budget is new authority and is deliberately
    not an M6 dashboard operation.
    """
    if _runtime_factory is not None and _configuration_path_for_tests is None:
        raise ValueError("configuration persistence is unavailable in injected runtime")
    if set(payload) != {"expected_configuration_digest", "poll_interval_seconds", "budgets"}:
        raise ValueError("configuration update contains unsupported fields")
    expected = payload["expected_configuration_digest"]
    if type(expected) is not str:
        raise ValueError("expected_configuration_digest is required")
    current = _configuration_with_digest(runtime.config)
    if expected != current["configuration_digest"]:
        raise RuntimeError("stale dashboard configuration; refresh before saving")
    interval = payload["poll_interval_seconds"]
    if type(interval) is not int or not 1 <= interval <= 3600:
        raise ValueError("poll_interval_seconds must be a bounded positive integer")
    budgets = payload["budgets"]
    if not isinstance(budgets, Mapping) or set(budgets) != set(current["budgets"]):
        raise ValueError("budgets must contain exactly the configured policy categories")
    for name, value in budgets.items():
        if type(value) is not int or value < 0:
            raise ValueError("budgets must be finite non-negative integers")
        if value > current["budgets"][name]:
            raise ValueError("dashboard configuration cannot increase a consumed-policy budget")
    path = _configuration_path_for_tests or _trusted_config_path()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("configuration must be valid JSON")
    # Re-open and compare while holding the coordinator singleton lock.  This
    # prevents a stale browser save from racing another dashboard mutation.
    raw = dict(raw)
    raw["poll_interval_seconds"] = interval
    raw["budgets"] = dict(budgets)
    replacement = PluginConfig.from_mapping(raw)
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".local-first-config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return _configuration_with_digest(replacement)


def dispatch(method: str, path: str, payload: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One mounted-request dispatch; every action has a fresh runtime and readback."""
    if method == "GET" and path == _PREFIX + "/status":
        try:
            runtime = _open_runtime()
            try:
                return 200, _status(runtime)
            finally:
                close_runtime(runtime)
        except (OSError, RuntimeError, ValueError) as exc:
            return _error(503, str(exc))
    if method == "GET" and path in {_PREFIX + "/profiles", _PREFIX + "/configuration"}:
        try:
            runtime = _open_runtime()
            try:
                if path.endswith("/profiles"):
                    return 200, {"scope": dict(runtime.scope), "profiles": dict(runtime.config.roles)}
                return 200, _configuration_with_digest(runtime.config)
            finally:
                close_runtime(runtime)
        except (OSError, RuntimeError, ValueError) as exc:
            return _error(503, str(exc))
    if method == "POST" and path in {_PREFIX + "/configuration", _PREFIX + "/update_configuration"}:
        if not isinstance(payload, Mapping):
            return _error(400, "dashboard configuration body must be an object")
        try:
            runtime = _open_runtime()
            try:
                with runtime.coordinator.lock:
                    configuration = _update_configuration(runtime, payload)
                    fresh = _status(runtime, configuration=configuration)
                    fresh["observation_digest"] = _digest({key: value for key, value in fresh.items() if key != "observation_digest"})
                    return 200, {"configuration": configuration, "status": fresh}
            finally:
                close_runtime(runtime)
        except RuntimeError as exc:
            return _error(409, str(exc))
        except (OSError, ValueError) as exc:
            return _error(400, str(exc))
    if method == "POST" and path == _PREFIX + "/enroll":
        if not isinstance(payload, Mapping) or set(payload) != {"expected_observation_digest"}:
            return _error(400, "enrollment requires only expected_observation_digest")
        try:
            expected = _expected_digest(payload)
            runtime = _open_runtime()
            try:
                before = _status(runtime)
                if expected != before["observation_digest"]:
                    return _error(409, "stale dashboard observation; refresh before mutating", fresh=before)
                result = runtime.coordinator.enroll()
                return 200, {"result": _plain(result), "status": _status(runtime)}
            finally:
                close_runtime(runtime)
        except (OSError, RuntimeError, ValueError) as exc:
            return _error(409, str(exc))
    if method != "POST" or not path.startswith(_PREFIX + "/actions/"):
        return _error(404, "dashboard route not found")
    action = path.removeprefix(_PREFIX + "/actions/")
    if action not in {"pause", "stop", "reconcile", "resume", "cancel", "recover"}:
        return _error(404, "unsupported dashboard action")
    if not isinstance(payload, Mapping):
        return _error(400, "dashboard action body must be an object")
    try:
        expected = _expected_digest(payload)
        runtime = _open_runtime()
        try:
            before = _status(runtime)
            if expected != before["observation_digest"]:
                return _error(409, "stale dashboard observation; refresh before mutating", fresh=before)
            coordinator = runtime.coordinator
            if action == "pause":
                result = coordinator.pause(stop=False)
            elif action == "stop":
                result = coordinator.pause(stop=True)
            elif action == "reconcile":
                result = coordinator.reconcile()
            elif action == "resume":
                if type(payload.get("authorized_clear", False)) is not bool:
                    return _error(400, "authorized_clear must be a boolean")
                result = coordinator.resume(authorized_clear=payload.get("authorized_clear", False))
            elif action == "cancel":
                result = coordinator.cancel()
            else:
                result = coordinator.recover()
            fresh = _status(runtime)
            return 200, {"result": _plain(result), "status": fresh}
        finally:
            close_runtime(runtime)
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(409, str(exc))


class _Router:
    """Minimal ASGI router so the plugin has no undeclared FastAPI dependency."""

    async def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Any], send: Callable[[Mapping[str, Any]], Any]) -> None:
        if scope.get("type") != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            payload = None if not body else json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            status, response = _error(400, "dashboard action body must be valid JSON")
        else:
            status, response = dispatch(str(scope.get("method", "")), str(scope.get("path", "")), payload)
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": encoded})


try:  # Hermes hosts normally provide FastAPI; fixture-only environments need no extra wheel.
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse
except ImportError:
    router = _Router()
else:
    router = APIRouter()

    @router.get("/status")
    def mounted_status():
        code, body = dispatch("GET", _PREFIX + "/status")
        return JSONResponse(status_code=code, content=body)

    @router.get("/profiles")
    def mounted_profiles():
        code, body = dispatch("GET", _PREFIX + "/profiles")
        return JSONResponse(status_code=code, content=body)

    @router.get("/configuration")
    def mounted_configuration():
        code, body = dispatch("GET", _PREFIX + "/configuration")
        return JSONResponse(status_code=code, content=body)

    @router.post("/configuration")
    @router.post("/update_configuration")
    def mounted_configuration_update(payload: dict[str, Any]):
        code, body = dispatch("POST", _PREFIX + "/configuration", payload)
        return JSONResponse(status_code=code, content=body)

    @router.post("/enroll")
    def mounted_enroll(payload: dict[str, Any]):
        code, body = dispatch("POST", _PREFIX + "/enroll", payload)
        return JSONResponse(status_code=code, content=body)

    @router.post("/actions/{action}")
    def mounted_action(action: str, payload: dict[str, Any]):
        code, body = dispatch("POST", _PREFIX + "/actions/" + action, payload)
        return JSONResponse(status_code=code, content=body)


__all__ = ["router", "dispatch", "configure_runtime_factory"]
