"""Authenticated operator API for one registered local-first ledger.

The dashboard mounts this router at ``/api/plugins/local-first-orchestrator``.
No HTTP parameter can choose a database, repository, provider, or model.
"""
from __future__ import annotations

import json

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
from local_first_orchestrator.hermes_profiles import discover_profiles, resolve_registration
from local_first_orchestrator.operator_config import OperatorConfig, default_config_path, load_operator_config, save_operator_config
from local_first_orchestrator.runtime_metrics import RuntimeMetricsStore

router = APIRouter()


class OperatorAction(BaseModel):
    reason: str = Field(default="operator action", max_length=240)


class ConfigurationUpdate(BaseModel):
    canonical_repository: str | None = Field(default=None, min_length=1, max_length=4096)
    repository_allowlist: list[str] | None = None
    implementation_profile: str = Field(min_length=1, max_length=240)
    review_profile: str = Field(min_length=1, max_length=240)
    decomposition_local_profile: str | None = Field(default=None, max_length=240)
    decomposition_standard_profile: str | None = Field(default=None, max_length=240)
    paid_checkpoint_profile: str | None = Field(default=None, max_length=240)
    paid_escalation_profile: str | None = Field(default=None, max_length=240)
    implementation_timeout_seconds: int = Field(ge=1, le=86400)
    review_timeout_seconds: int = Field(ge=1, le=86400)


class ImplementationAction(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=240)
    reason: str = Field(default="operator implementation-only action", max_length=240)


class RevalidateImplementationAction(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=240)
    attempt_number: int = Field(ge=1)
    reason: str = Field(default="operator revalidate existing implementation", max_length=240)


class AuthorizeHistoricalRevalidationAction(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=240)
    attempt_number: int = Field(ge=1)
    reason: str = Field(default="operator authorization for historical revalidation", max_length=240)


class AttestHistoricalRevalidationAction(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=240)
    attempt_number: int = Field(ge=1)


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


def _configuration_json(config: OperatorConfig) -> dict[str, Any]:
    decomposition = dict(config.decomposition)
    return {
        "canonical_repository": str(config.canonical_repository),
        "repository_allowlist": [str(path) for path in config.repository_allowlist],
        "implementation": config.implementation.__dict__,
        "review": config.review.__dict__,
        "decomposition": {name: route.__dict__ for name, route in decomposition.items()},
        "paid_checkpoint": config.paid_checkpoint.__dict__ if config.paid_checkpoint is not None else None,
        "paid_escalation": config.paid_escalation.__dict__ if config.paid_escalation is not None else None,
        "worktree_root": str(config.worktree_root) if config.worktree_root is not None else None,
        "artifact_root": str(config.artifact_root) if config.artifact_root is not None else None,
        "implementation_timeout_seconds": config.implementation_timeout_seconds,
        "review_timeout_seconds": config.review_timeout_seconds,
    }


def _optional_registration(profile: str | None):
    if profile is None or not profile.strip():
        return None
    return resolve_registration(profile.strip())


def _status() -> dict[str, Any]:
    ledger, config = _ledger()
    try:
        status = ledger.operator_status(active_limit=25)
        status["configuration"] = _configuration_json(config)
        metrics = RuntimeMetricsStore(ledger)
        status["runtime_metrics"] = metrics.summary()
        status["adaptive_sizing"] = metrics.recommendation().as_json()
        return status
    finally:
        ledger.close()

@router.get("/status")
def status() -> dict[str, Any]:
    """Read bounded operator status from the single registered ledger."""
    return _status()


@router.get("/profiles")
def profiles() -> dict[str, Any]:
    """Discover Hermes profiles and their resolved provider/model provenance."""
    try:
        return {"profiles": [profile.as_json() for profile in discover_profiles()]}
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Hermes profiles unavailable: {exc}") from exc


@router.put("/configuration")
def update_configuration(update: ConfigurationUpdate) -> dict[str, Any]:
    """Update model-role routing from Hermes profile selections while paused."""
    ledger, config = _ledger()
    try:
        if not ledger.status()["paused"]:
            raise HTTPException(status_code=409, detail="pause Local First before changing model-role configuration")
        routes = {
            name: registration
            for name, registration in (
                ("local", _optional_registration(update.decomposition_local_profile)),
                ("standard", _optional_registration(update.decomposition_standard_profile)),
            )
            if registration is not None
        }
        requested_repository = Path(update.canonical_repository).expanduser() if update.canonical_repository is not None else config.canonical_repository
        requested_allowlist = tuple(Path(item).expanduser() for item in update.repository_allowlist) if update.repository_allowlist is not None else config.repository_allowlist
        if update.repository_allowlist is not None and (not update.repository_allowlist or any(not item.strip() for item in update.repository_allowlist)):
            raise ValueError("repository_allowlist must be a non-empty list of paths")
        checked = OperatorConfig(
            ledger_path=config.ledger_path,
            canonical_repository=requested_repository,
            repository_allowlist=requested_allowlist,
            implementation=resolve_registration(update.implementation_profile),
            review=resolve_registration(update.review_profile),
            worktree_root=config.worktree_root,
            artifact_root=config.artifact_root,
            implementation_timeout_seconds=update.implementation_timeout_seconds,
            review_timeout_seconds=update.review_timeout_seconds,
            decomposition=tuple(sorted(routes.items())),
            paid_checkpoint=_optional_registration(update.paid_checkpoint_profile),
            paid_escalation=_optional_registration(update.paid_escalation_profile),
        ).validated(require_ledger=True)
        save_operator_config(checked, default_config_path())
        ledger.connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,from_state,to_state,actor_type,actor_id,payload_json,created_at) "
            "VALUES ('controller','global','operator_configuration_updated',NULL,NULL,'controller','dashboard-operator',?,strftime('%s','now'))",
            (json.dumps({
                "implementation_profile": checked.implementation.profile,
                "canonical_repository": str(checked.canonical_repository),
                "repository_allowlist": [str(path) for path in checked.repository_allowlist],
                "review_profile": checked.review.profile,
                "decomposition_profiles": {name: route.profile for name, route in checked.decomposition},
                "paid_checkpoint_profile": checked.paid_checkpoint.profile if checked.paid_checkpoint else None,
                "paid_escalation_profile": checked.paid_escalation.profile if checked.paid_escalation else None,
            }, sort_keys=True),),
        )
        return _configuration_json(checked)
    except HTTPException:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        ledger.close()


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


@router.post("/implementation-revalidate")
def implementation_revalidate(action: RevalidateImplementationAction) -> dict[str, Any]:
    """Revalidate one preserved implementation; never invokes a model."""
    ledger, config = _ledger()
    try:
        runtime = config.runtime_config()
        return LocalFirstController(ledger, _OperatorBoard(), runtime).revalidate_historical_implementation(
            action.ticket_id, action.attempt_number, repository=config.canonical_repository, operator_id="dashboard-operator")
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        ledger.close()


@router.post("/implementation-revalidation-authorize")
def implementation_revalidation_authorize(action: AuthorizeHistoricalRevalidationAction) -> dict[str, Any]:
    """Authorize one exact historical implementation for a future integrity gate."""
    ledger, config = _ledger()
    try:
        runtime = config.runtime_config()
        return LocalFirstController(ledger, _OperatorBoard(), runtime).authorize_historical_revalidation(
            action.ticket_id, action.attempt_number, repository=config.canonical_repository,
            operator_id="dashboard-operator", reason=action.reason)
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        ledger.close()


@router.post("/implementation-revalidation-attest")
def implementation_revalidation_attest(action: AttestHistoricalRevalidationAction) -> dict[str, Any]:
    """Attest preserved implementation identity; never validates semantics."""
    ledger, config = _ledger()
    try:
        runtime = config.runtime_config()
        return LocalFirstController(ledger, _OperatorBoard(), runtime).attest_historical_revalidation_implementation(
            action.ticket_id, action.attempt_number, repository=config.canonical_repository, operator_id="dashboard-operator")
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        ledger.close()
