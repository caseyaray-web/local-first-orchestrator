"""Minimal authenticated operator API for the separate local-first ledger.

The Hermes dashboard mounts this router at
``/api/plugins/local-first-orchestrator``.  It never imports Hermes board
state, registers model tools, or invokes a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Dashboard backend modules are loaded directly from this directory.  Make the
# trusted, installed plugin root importable without depending on dashboard CWD.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from local_first_orchestrator.ledger import Ledger

router = APIRouter()


class OperatorAction(BaseModel):
    reason: str = Field(default="operator action", max_length=240)


def _ledger(database: str) -> Ledger:
    path = Path(database).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="ledger database does not exist")
    try:
        ledger = Ledger(path)
        ledger.migrate()
        return ledger
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid ledger database: {exc}") from exc


def _status(database: str) -> dict[str, int | bool]:
    ledger = _ledger(database)
    try:
        return ledger.operator_status()
    finally:
        ledger.close()


@router.get("/status")
def status(database: str = Query(..., min_length=1)) -> dict[str, int | bool]:
    """Read bounded operator state only; ticket IDs and evidence stay private."""
    return _status(database)


@router.post("/pause")
def pause(action: OperatorAction, database: str = Query(..., min_length=1)) -> dict[str, int | bool]:
    ledger = _ledger(database)
    try:
        ledger.pause("dashboard-operator", reason=action.reason)
        return ledger.operator_status()
    finally:
        ledger.close()


@router.post("/resume")
def resume(action: OperatorAction, database: str = Query(..., min_length=1)) -> dict[str, int | bool]:
    ledger = _ledger(database)
    try:
        ledger.resume("dashboard-operator", reason=action.reason)
        return ledger.operator_status()
    finally:
        ledger.close()
