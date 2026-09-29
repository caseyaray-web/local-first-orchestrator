"""Pure, finite accounting policy for root-and-finding recovery work.

The evidence store is the durable source of consumed units.  This module does
not create native work or infer a board lifecycle; it only decides whether a
proven native effect may consume one configured unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, cast

from .contracts import ConflictError, OperationIntent, validate_scope

GENERAL_ATTEMPT = "__general_attempt__"
IMPLEMENTATION_ATTEMPTS = "implementation_attempts"
REVIEW_CORRECTIONS = "review_corrections"
INFRASTRUCTURE_RETRIES = "infrastructure_retries"
WORKFLOW_REPAIRS = "workflow_repairs"
PAID_CAPACITY = "paid_capacity"
CATEGORIES = frozenset(
    {
        IMPLEMENTATION_ATTEMPTS,
        REVIEW_CORRECTIONS,
        INFRASTRUCTURE_RETRIES,
        WORKFLOW_REPAIRS,
        PAID_CAPACITY,
    }
)


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Explicit, finite limits; callers must configure every category."""

    implementation_attempts: int
    review_corrections: int
    infrastructure_retries: int
    workflow_repairs: int
    paid_capacity: int

    def __post_init__(self) -> None:
        for category in CATEGORIES:
            value = getattr(self, category)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("budget limits must be non-negative integers")

    def limit(self, category: str) -> int:
        _validate_category(category)
        return getattr(self, category)

    def remaining(
        self,
        store: Any,
        scope: Mapping[str, Any],
        category: str,
        *,
        finding_id: str = GENERAL_ATTEMPT,
    ) -> int:
        return remaining(self, store, scope, category, finding_id=finding_id)

    def record_once(self, store: Any, scope: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        return record_once(store, scope, event, policy=self)

    def admit_repair_operation(
        self,
        store: Any,
        scope: Mapping[str, Any],
        intent: OperationIntent,
        event: Mapping[str, Any],
    ) -> OperationIntent:
        return admit_repair_operation(self, store, scope, intent, event)

    def permit_action(
        self,
        store: Any,
        scope: Mapping[str, Any],
        category: str,
        *,
        finding_id: str = GENERAL_ATTEMPT,
        paid_authorized: bool = False,
    ) -> bool:
        return permit_action(
            self,
            store,
            scope,
            category,
            finding_id=finding_id,
            paid_authorized=paid_authorized,
        )

    def explain_exhaustion(
        self,
        store: Any,
        scope: Mapping[str, Any],
        category: str,
        *,
        finding_id: str = GENERAL_ATTEMPT,
    ) -> str:
        return explain_exhaustion(self, store, scope, category, finding_id=finding_id)


def _validate_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unsupported budget category: {category!r}")


def _validate_finding_id(finding_id: str) -> None:
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError("finding_id must be a non-empty string")


def _event_category(event: Mapping[str, Any]) -> str:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or ":" not in event_id:
        raise ValueError("budget event_id must begin with a budget category and colon")
    category, suffix = event_id.split(":", 1)
    _validate_category(category)
    if not suffix:
        raise ValueError("budget event_id requires a stable source suffix")
    return category


def classify_run_start(run: Mapping[str, Any]) -> str:
    """Classify native-run evidence without treating a pre-start failure as work.

    ``unknown_active`` deliberately consumes an attempt when recorded: after a
    crash or incomplete native observation, re-dispatching is less safe than
    preserving the possible started run until reconciliation proves otherwise.
    """

    if not isinstance(run, Mapping):
        raise ValueError("run observation must be a mapping")
    if run.get("started") is True:
        return "confirmed_started"
    if run.get("started_at") not in (None, "") or run.get("start_time") not in (None, ""):
        return "confirmed_started"
    status = run.get("status", run.get("state", run.get("phase")))
    if status in {"running", "completed", "succeeded", "finished"}:
        return "confirmed_started"
    if status in {"failed", "rejected", "cancelled"} and run.get("started_at", "missing") is None:
        return "pre_start_failure"
    return "unknown_active"


def _stored_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Remove local-only run observation before passing the fixed store schema."""

    if not isinstance(event, Mapping):
        raise ValueError("budget event must be a mapping")
    stored = dict(event)
    stored.pop("run", None)
    _event_category(stored)
    return stored


def record_once(store: Any, scope: Mapping[str, Any], event: Mapping[str, Any], *, policy: BudgetPolicy) -> bool:
    """Persist one proven native source exactly once; return whether it was new.

    EvidenceStore validates root, member generation, and finding membership.
    Looking up an existing native source first makes replacement-card retries
    idempotent without accepting contradictory lineage attribution.
    """

    valid_scope = validate_scope(scope)
    stored = _stored_event(event)
    if stored.get("source_kind") != "native_run" or "run" not in event:
        raise ValueError("budget admission requires a native run observation")
    classification = classify_run_start(event["run"])
    if classification == "pre_start_failure":
        return False
    if stored.get("count") != 1:
        raise ValueError("each budget event must consume exactly one unit")

    native_source_id = stored.get("native_source_id")
    if not isinstance(native_source_id, str):
        raise ValueError("budget event requires a native source")
    try:
        store.record_budget_event(valid_scope, stored, policy_limit=policy.limit(_event_category(stored)), run_classification=classification)
        return True
    except ConflictError as error:
        # Same immutable source is idempotent only when its durable attribution
        # matches; every other conflict, including an exhausted cap, is loud.
        for previous in store.read_scope(valid_scope)["budget_events"]:
            if previous["native_source_id"] == native_source_id and previous["source_kind"] == "native_run":
                if previous["root_task_id"] == stored["root_task_id"] and previous["finding_id"] == stored["finding_id"] and _event_category(previous) == _event_category(stored):
                    return False
        raise error


def admit_repair_operation(
    policy: BudgetPolicy,
    store: Any,
    scope: Mapping[str, Any],
    intent: OperationIntent,
    event: Mapping[str, Any],
) -> OperationIntent:
    """Atomically reserve one future repair effect and consume its finite unit.

    This is deliberately separate from :func:`record_once`: a repair operation
    must be admitted before native I/O, whereas ``record_once`` records an
    observed native run after its conservative start classification.  M2
    containment, read-only reconciliation, and authorized resume do not call
    this API and remain usable when repair capacity is exhausted.
    """

    if not isinstance(policy, BudgetPolicy):
        raise ValueError("budgeted repair admission requires an explicit BudgetPolicy")
    valid_scope = validate_scope(scope)
    if not isinstance(intent, OperationIntent) or validate_scope(intent.scope) != valid_scope:
        raise ValueError("budgeted repair operation must have the exact requested scope")
    if not isinstance(event, Mapping):
        raise ValueError("budgeted repair operation requires a budget event")
    task_id = intent.target.get("task_id")
    required = {
        "event_id", "lineage_id", "root_task_id", "finding_id", "generation",
        "source_task_id", "source_kind", "native_source_id", "count",
    }
    if set(event) != required or not isinstance(task_id, str) or not task_id:
        raise ValueError("budgeted repair operation requires exact operation attribution")
    if (
        _event_category(event) != WORKFLOW_REPAIRS
        or event["event_id"] != f"{WORKFLOW_REPAIRS}:{intent.key}"
        or event["source_kind"] != "native_operation"
        or event["native_source_id"] != intent.key
        or event["source_task_id"] != task_id
        or event["count"] != 1
    ):
        raise ValueError("budgeted repair operation event must exactly bind its operation key and task")
    reserve = getattr(store, "reserve_budgeted_repair_operation", None)
    if not callable(reserve):
        raise ValueError("budgeted repair admission requires durable operation reservation")
    return cast(OperationIntent, reserve(intent, dict(event), policy_limit=policy.limit(WORKFLOW_REPAIRS)))


def remaining(
    policy: BudgetPolicy,
    store: Any,
    scope: Mapping[str, Any],
    category: str,
    *,
    finding_id: str = GENERAL_ATTEMPT,
) -> int:
    """Return units left for one root-and-finding lineage, never a task ID."""

    _validate_category(category)
    _validate_finding_id(finding_id)
    valid_scope = validate_scope(scope)
    state = store.read_scope(valid_scope)
    consumed = sum(
        row["net"]
        for row in state.get("budget_net", ())
        if row["root_task_id"] == valid_scope["anchor_task_id"]
        and row["finding_id"] == finding_id
        and row["category"] == category
    )
    return max(0, policy.limit(category) - consumed)


def permit_action(
    policy: BudgetPolicy,
    store: Any,
    scope: Mapping[str, Any],
    category: str,
    *,
    finding_id: str = GENERAL_ATTEMPT,
    paid_authorized: bool = False,
) -> bool:
    """Allow only configured remaining capacity and explicitly authorized paid work."""

    _validate_category(category)
    if category == PAID_CAPACITY and not paid_authorized:
        return False
    return remaining(policy, store, scope, category, finding_id=finding_id) > 0


def explain_exhaustion(
    policy: BudgetPolicy,
    store: Any,
    scope: Mapping[str, Any],
    category: str,
    *,
    finding_id: str = GENERAL_ATTEMPT,
) -> str:
    """Produce the one actionable hold reason for a blocked root/finding line."""

    valid_scope = validate_scope(scope)
    units = remaining(policy, store, valid_scope, category, finding_id=finding_id)
    if units:
        return f"{category} has {units} unit(s) remaining for root {valid_scope['anchor_task_id']} finding {finding_id}."
    return (
        f"hold root {valid_scope['anchor_task_id']} finding {finding_id}: {category} is exhausted. "
        "Require explicit operator review before changing configured limits or releasing more work."
    )
