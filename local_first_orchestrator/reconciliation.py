from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReconciliationState(StrEnum):
    NOT_STARTED = "not_started"
    STARTED_EXTERNAL_OUTCOME_UNKNOWN = "started_external_outcome_unknown"
    EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE = "external_effect_completed_local_incomplete"
    LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE = "local_stage_completion_recorded_downstream_incomplete"
    FULLY_FINALIZED = "fully_finalized"


class ReconciliationAction(StrEnum):
    RESUME = "resume"
    REPLAY = "replay"
    RECONCILE = "reconcile"
    RETRY = "retry"
    STOP = "stop"


@dataclass(frozen=True)
class SchedulerReconciliationDecision:
    claim_id: str
    ticket_id: str
    stage: str
    state: ReconciliationState
    action: ReconciliationAction
    evidence_kind: str
    reason: str
    evidence: dict[str, Any]
