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


@dataclass(frozen=True)
class SchedulerCrashPolicy:
    stage: str
    started_action: ReconciliationAction
    external_completed_action: ReconciliationAction = ReconciliationAction.RECONCILE
    downstream_incomplete_action: ReconciliationAction = ReconciliationAction.RESUME
    finalized_action: ReconciliationAction = ReconciliationAction.RESUME
    authority: str = "scheduler_stage_claims"


def _policy(stage: str, started_action: ReconciliationAction, authority: str = "scheduler_stage_claims") -> SchedulerCrashPolicy:
    return SchedulerCrashPolicy(stage=stage, started_action=started_action, authority=authority)


SCHEDULER_CRASH_POLICIES: dict[str, SchedulerCrashPolicy] = {
    "generated_projection": _policy("generated_projection", ReconciliationAction.REPLAY, "board_projection_outbox"),
    "state_projection": _policy("state_projection", ReconciliationAction.REPLAY, "board_projection_outbox"),
    "evidence_comment": _policy("evidence_comment", ReconciliationAction.REPLAY, "evidence_comment_outbox"),
    "implementation": _policy("implementation", ReconciliationAction.STOP, "model_invocations"),
    "validation": _policy("validation", ReconciliationAction.REPLAY),
    "review": _policy("review", ReconciliationAction.STOP, "model_invocations"),
    "repair_routing": _policy("repair_routing", ReconciliationAction.REPLAY),
    "ticket_paid_escalation": _policy("ticket_paid_escalation", ReconciliationAction.STOP, "paid_reservations"),
    "triage": _policy("triage", ReconciliationAction.STOP, "model_invocations"),
    "acceptance": _policy("acceptance", ReconciliationAction.REPLAY),
    "git_integration": _policy("git_integration", ReconciliationAction.REPLAY, "git_commit_intents"),
    "completion": _policy("completion", ReconciliationAction.REPLAY),
    "worktree_cleanup": _policy("worktree_cleanup", ReconciliationAction.REPLAY, "runtime_stages"),
    "native_dependency_graph": _policy("native_dependency_graph", ReconciliationAction.REPLAY, "native_dependency_graphs"),
    "native_dependency_release": _policy("native_dependency_release", ReconciliationAction.REPLAY, "native_dependency_releases"),
    "tranche_checkpoint": _policy("tranche_checkpoint", ReconciliationAction.REPLAY, "tranche_checkpoint_evidence"),
    "paid_checkpoint": _policy("paid_checkpoint", ReconciliationAction.STOP, "paid_reservations"),
    "paid_escalation": _policy("paid_escalation", ReconciliationAction.STOP, "paid_reservations"),
    "next_tranche_materialize": _policy("next_tranche_materialize", ReconciliationAction.REPLAY, "next_tranche_materializations"),
    "next_tranche_activation": _policy("next_tranche_activation", ReconciliationAction.REPLAY, "next_tranche_activation_evidence"),
    "dependency_readiness": _policy("dependency_readiness", ReconciliationAction.REPLAY),
}


CRASH_BOUNDARY_ACTIONS: tuple[tuple[ReconciliationState, ReconciliationAction], ...] = (
    (ReconciliationState.NOT_STARTED, ReconciliationAction.RETRY),
    (ReconciliationState.EXTERNAL_EFFECT_COMPLETED_LOCAL_INCOMPLETE, ReconciliationAction.RECONCILE),
    (ReconciliationState.LOCAL_STAGE_COMPLETION_RECORDED_DOWNSTREAM_INCOMPLETE, ReconciliationAction.RESUME),
    (ReconciliationState.FULLY_FINALIZED, ReconciliationAction.RESUME),
)
