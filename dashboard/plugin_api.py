"""Authenticated operator API for one registered local-first ledger.

The dashboard mounts this router at ``/api/plugins/local-first-orchestrator``.
No HTTP parameter can choose a database, repository, provider, or model.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.controller import LocalFirstController
from local_first_orchestrator.local_qwen import LocalQwenAdapter
from local_first_orchestrator.operator_config import OperatorConfig, load_operator_config

router = APIRouter()


class OperatorAction(BaseModel):
    reason: str = Field(default="operator action", max_length=240)


class ImplementationAction(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=240)
    reason: str = Field(default="operator implementation-only action", max_length=240)


class _OperatorBoard:
    is_fake = False


def _config() -> OperatorConfig:
    try:
        return load_operator_config()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"operator dashboard unavailable: {exc}") from exc


def _ledger() -> tuple[Ledger, OperatorConfig]:
    config = _config()
    try:
        ledger = Ledger(config.ledger_path)
        ledger.migrate()
        return ledger, config
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"registered ledger unavailable: {exc}") from exc


def _status() -> dict[str, Any]:
    ledger, config = _ledger()
    try:
        status = ledger.operator_status(active_limit=25)
        status["configuration"] = {
            "canonical_repository": str(config.canonical_repository),
            "repository_allowlist": [str(path) for path in config.repository_allowlist],
            "implementation": config.implementation.__dict__,
            "review": config.review.__dict__,
            "worktree_root": str(config.worktree_root) if config.worktree_root is not None else None,
            "artifact_root": str(config.artifact_root) if config.artifact_root is not None else None,
            "implementation_timeout_seconds": config.implementation_timeout_seconds,
            "review_timeout_seconds": config.review_timeout_seconds,
        }
        return status
    finally:
        ledger.close()

@router.get("/status")
def status() -> dict[str, Any]:
    """Read bounded operator status from the single registered ledger."""
    return _status()


@router.post("/pause")
def pause(action: OperatorAction) -> dict[str, Any]:
    ledger, _ = _ledger()
    try:
        ledger.pause("dashboard-operator", reason=action.reason)
    finally:
        ledger.close()
    return _status()


@router.post("/resume")
def resume(action: OperatorAction) -> dict[str, Any]:
    ledger, _ = _ledger()
    try:
        ledger.resume("dashboard-operator", reason=action.reason)
    finally:
        ledger.close()
    return _status()


@router.post("/implementation")
def implementation(action: ImplementationAction) -> dict[str, Any]:
    """Run one registered ticket through implementation and validation only."""
    ledger, config = _ledger()
    try:
        runtime = config.runtime_config()
        model = LocalQwenAdapter(provider=config.implementation.provider, model=config.implementation.model,
                                 hermes_home=Path.home()/".hermes"/"profiles"/config.implementation.profile,
                                 implementation_timeout_seconds=runtime.implementation_timeout_seconds,
                                 review_timeout_seconds=runtime.review_timeout_seconds)
        result = LocalFirstController(ledger, _OperatorBoard(), runtime, local_model=model).execute_implementation(
            action.ticket_id, repository=config.canonical_repository, owner="dashboard-operator")
        return result or {"ticket_id": action.ticket_id, "status": "not_run"}
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        ledger.close()
