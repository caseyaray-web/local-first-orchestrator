"""Scoped dashboard API backed only by the plugin composition root.

The host mounts ``router`` below ``/api/plugins/local-first-orchestrator``.
Browser payloads can select a named operator action, but never a repository,
executable, database, profile, budget, board, or anchor.  Those values come
from the trusted local PluginConfig and trusted bootstrap scope.
"""
from __future__ import annotations

from dataclasses import is_dataclass
import hashlib
import json
import os
import sys
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


def _trusted_runtime() -> Runtime:
    """Compose from server-local bootstrap only; request data is never consulted."""
    raw_config = os.environ.get("LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    if not raw_config:
        raise ValueError("dashboard requires trusted LOCAL_FIRST_ORCHESTRATOR_CONFIG")
    config = PluginConfig.from_file(Path(raw_config))
    return build_runtime(config, scope=config.scope)


def configure_runtime_factory(factory: RuntimeFactory | None) -> None:
    """Test-only injection point; production leaves this unset."""
    global _runtime_factory
    _runtime_factory = factory


def _open_runtime() -> Runtime:
    return _trusted_runtime() if _runtime_factory is None else _runtime_factory()


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _status(runtime: Runtime) -> dict[str, Any]:
    observed = _plain(runtime.coordinator.status())
    members = observed.get("members", [])
    native_tasks = observed.get("native_tasks", {})
    native_runs = observed.get("native_runs", [])
    operations = observed.get("operations", [])
    reviews = observed.get("reviews", [])
    budget_events = observed.get("budget_events", [])
    intent = observed.get("operator_intent")
    active_workers = [run for run in native_runs if run.get("status") in {"active", "claimed", "running", "stopping"}]
    preserved_paths = sorted({str(value) for item in operations for value in (
        item.get("target", {}).get("worktree"), item.get("target", {}).get("workspace"),
        item.get("readback", {}).get("worktree") if isinstance(item.get("readback"), dict) else None,
    ) if isinstance(value, str) and value})
    projection = {
        "scope": observed.get("scope", dict(runtime.scope)),
        "anchor": native_tasks.get(runtime.scope["anchor_task_id"]),
        "managed_anchors": members,
        "current_head": observed.get("git_observation"),
        "reviews": reviews,
        "review_queues": {
            "local": [item for item in reviews if item.get("role") == "local_review"],
            "paid": [item for item in reviews if item.get("role") == "paid_review"],
        },
        "repair_history": operations[-50:],
        "budgets": budget_events,
        "operator_intent": intent,
        "active_workers": active_workers,
        "uncontained_workers": active_workers if intent and intent.get("active") else [],
        "preserved_paths": preserved_paths,
        "pending_operations": [item for item in operations if item.get("phase") in {"pending", "unknown"}],
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

    @router.post("/actions/{action}")
    def mounted_action(action: str, payload: dict[str, Any]):
        code, body = dispatch("POST", _PREFIX + "/actions/" + action, payload)
        return JSONResponse(status_code=code, content=body)


__all__ = ["router", "dispatch", "configure_runtime_factory"]
