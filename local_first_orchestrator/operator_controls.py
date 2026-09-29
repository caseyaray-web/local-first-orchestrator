"""Pure scoped operator-control planning and containment verification.

The coordinator owns persistence and native effects.  This module only turns
explicit observations into PauseIntent records, supported Actions, and honest
outcome decisions; it never reads a board, process table, store, or repository.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import Action, ActionResult, BoardSnapshot, ManagedMember, PauseIntent, RecoveryReport, validate_scope

_ACTIVE_RUN_STATUSES = frozenset({"active", "claimed", "running", "stopping"})
_HELD_TASK_STATES = frozenset({"held", "paused", "parked", "waiting"})
_TERMINAL_TASK_STATES = frozenset({"cancelled", "completed", "done", "stopped"})


@dataclass(frozen=True, slots=True)
class OperatorPlan:
    """A requested durable intent plus actions for the coordinator to apply later."""

    intent: PauseIntent
    actions: tuple[Action, ...]
    report: RecoveryReport
    outcome: str


@dataclass(frozen=True, slots=True)
class ContainmentVerification:
    outcome: str
    report: RecoveryReport


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    outcome: str
    actions: tuple[Action, ...]
    report: RecoveryReport


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    allowed: bool
    reason: str
    actions: tuple[Action, ...] = ()


@dataclass(frozen=True, slots=True)
class LateResultDecision:
    rejected: bool
    reason: str
    action: Action | None = None


def _scope(scope: Mapping[str, Any]) -> dict[str, str]:
    return validate_scope(scope)


def _member_snapshots(scope: Mapping[str, str], members: tuple[ManagedMember, ...], snapshots: tuple[BoardSnapshot, ...]) -> tuple[tuple[ManagedMember, BoardSnapshot | None], ...]:
    selected = tuple(member for member in members if member.board_id == scope["board_id"] and member.anchor_task_id == scope["anchor_task_id"])
    by_task = {str(snapshot.native_task.get("id")): snapshot for snapshot in snapshots if isinstance(snapshot.native_task.get("id"), str)}
    return tuple((member, by_task.get(member.task_id)) for member in selected)


def _run_id(run: Mapping[str, Any]) -> str | None:
    value = run.get("id")
    return value if isinstance(value, str) and value else None


def _run_is_live(run: Mapping[str, Any]) -> bool:
    return run.get("status") in _ACTIVE_RUN_STATUSES


def _active_runs(snapshots: tuple[BoardSnapshot, ...], runs: tuple[Mapping[str, Any], ...] = ()) -> tuple[str, ...]:
    observed: dict[str, Mapping[str, Any]] = {}
    for snapshot in snapshots:
        for run in snapshot.runs:
            run_id = _run_id(run)
            if run_id:
                observed[run_id] = run
    for run in runs:
        run_id = _run_id(run)
        if run_id:
            observed[run_id] = run
    return tuple(sorted(run_id for run_id, run in observed.items() if _run_is_live(run)))


def _snapshots_by_task(snapshots: tuple[BoardSnapshot, ...]) -> dict[str, BoardSnapshot]:
    return {
        task_id: snapshot
        for snapshot in snapshots
        if isinstance((task_id := snapshot.native_task.get("id")), str) and task_id
    }


def _stop_result_matches(result: ActionResult, task_id: str, run_id: str) -> bool:
    """Accept only coordinator readback proving this exact captured run exited."""
    readback = result.readback
    if result.outcome != "verified" or readback is None:
        return False
    return (
        result.action_key.startswith(f"stop_run:{task_id}:{run_id}:")
        and readback.get("task_id") == task_id
        and readback.get("run_id") == run_id
        and readback.get("stop_supported") is True
        and readback.get("process_exited") is True
        and readback.get("status") in _TERMINAL_TASK_STATES
    )


def _stopped_in_snapshot(snapshot: BoardSnapshot, run_id: str) -> bool:
    return any(_run_id(run) == run_id and run.get("status") in _TERMINAL_TASK_STATES for run in snapshot.runs)


def _report(scope: Mapping[str, str], problem: str, *, actions: tuple[Action, ...] = (), preserved: tuple[str, ...] = (), active: tuple[str, ...] = (), required: str | None = None) -> RecoveryReport:
    return RecoveryReport(scope, problem, tuple(action.key for action in actions), preserved, active, required)


def plan_stop(run: Mapping[str, Any], scope: Mapping[str, Any] | None = None, *, expected_observed_identity: str | None = None) -> Action | None:
    """Propose an exact-run stop only when the observation supports that identity.

    ``None`` means an exact supported stop cannot be planned; callers must report
    partial containment rather than claiming that a process was stopped.
    """
    resolved_scope = _scope(scope if scope is not None else run.get("scope", {}))
    run_id = _run_id(run)
    if run_id is None or not _run_is_live(run) or run.get("stop_supported") == "false":
        return None
    task_id = run.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    identity = expected_observed_identity or run.get("observed_identity")
    if not isinstance(identity, str) or not identity:
        return None
    return Action(f"stop_run:{task_id}:{run_id}:{identity}", resolved_scope,
                  {"task_id": task_id, "run_id": run_id}, "stop_run", identity)


def plan_pause(scope: Mapping[str, Any], stop: bool, *, generation: int = 0,
               cancellation: bool = False, members: tuple[ManagedMember, ...] = (),
               snapshots: tuple[BoardSnapshot, ...] = ()) -> OperatorPlan:
    """Plan a persisted operator intent and bounded native containment actions.

    The returned intent must be persisted by the coordinator before any Action is
    attempted.  Missing observations, claim races, and live workers are partial,
    never a successful stop.
    """
    resolved_scope = _scope(scope)
    intent = PauseIntent(resolved_scope, "operator", generation, stop, cancellation)
    actions: list[Action] = []
    active: list[str] = []
    partial = False
    for member, observed in _member_snapshots(resolved_scope, members, snapshots):
        if observed is None:
            partial = True
            continue
        live = _active_runs((observed,))
        active.extend(live)
        state = observed.native_task.get("state")
        if live:
            partial = True
            if stop:
                by_id = {str(run.get("id")): run for run in observed.runs}
                for run_id in live:
                    action = plan_stop({**dict(by_id[run_id]), "task_id": member.task_id}, resolved_scope,
                                       expected_observed_identity=observed.digest)
                    if action is not None:
                        actions.append(action)
            elif state == "claimed":
                # A hold may lose the independent-dispatch race; it is still the
                # only supported proposed containment, with partial reported.
                actions.append(Action(f"hold:{member.task_id}:{observed.digest}", resolved_scope,
                                      {"task_id": member.task_id}, "hold", observed.digest))
        elif state not in _HELD_TASK_STATES and state not in _TERMINAL_TASK_STATES:
            actions.append(Action(f"hold:{member.task_id}:{observed.digest}", resolved_scope,
                                  {"task_id": member.task_id}, "hold", observed.digest))
    active_ids = tuple(sorted(set(active)))
    outcome = "partial" if partial else ("pending" if actions else "no-op")
    required = "verify_exact_holds_and_runs" if outcome == "partial" else None
    return OperatorPlan(intent, tuple(actions), _report(resolved_scope, "operator_pause_planned", actions=tuple(actions), active=active_ids, required=required), outcome)


def verify_containment(scope: Mapping[str, Any], members: tuple[ManagedMember, ...],
                       snapshots: tuple[BoardSnapshot, ...], runs: tuple[Mapping[str, Any], ...], *,
                       pause_intent: PauseIntent | None = None,
                       prior_snapshots: tuple[BoardSnapshot, ...] = (),
                       effect_results: tuple[ActionResult, ...] = ()) -> ContainmentVerification:
    """Verify exact observed containment; it does not infer effects from intent.

    ``effect_results`` is explicit coordinator readback input, not an assertion
    that a requested effect occurred.  Ambiguous or unknown results keep the
    scope uncontained until a later observation resolves them.
    """
    resolved_scope = _scope(scope)
    active = _active_runs(snapshots, runs)
    unresolved_effect = any(result.outcome in {"ambiguous", "unknown", "partial"} for result in effect_results)
    incomplete = unresolved_effect
    current = _snapshots_by_task(snapshots)
    prior = _snapshots_by_task(prior_snapshots)
    selected = _member_snapshots(resolved_scope, members, snapshots)
    stop_requested = pause_intent is not None and pause_intent.stop_requested
    for member, observed in selected:
        if observed is None:
            incomplete = True
            continue
        state = observed.native_task.get("state")
        if state not in _HELD_TASK_STATES and state not in _TERMINAL_TASK_STATES:
            incomplete = True
        if not stop_requested:
            continue
        before = prior.get(member.task_id)
        # A held later snapshot is not proof that M0 stopped a run.  A stop
        # request requires a captured before observation even when it showed no
        # live run, so the coordinator cannot silently fill that gap with an
        # empty current run list.
        if before is None:
            incomplete = True
            continue
        for run in before.runs:
            run_id = _run_id(run)
            if run_id is None or not _run_is_live(run):
                continue
            if not _stopped_in_snapshot(current[member.task_id], run_id):
                incomplete = True
                continue
            if not any(_stop_result_matches(result, member.task_id, run_id) for result in effect_results):
                incomplete = True
    if active or incomplete:
        return ContainmentVerification("partial", _report(resolved_scope, "containment_unverified", active=active, required="inspect_exact_task_and_run_ids"))
    return ContainmentVerification("verified", _report(resolved_scope, "containment_verified"))


def _native_change(before: BoardSnapshot, after: BoardSnapshot) -> bool:
    """Compare only native control fields, not observation metadata or digest."""
    for field in ("state", "lane", "route", "dependencies"):
        if before.native_task.get(field) != after.native_task.get(field):
            return True
    def run_shape(run: Mapping[str, Any]) -> tuple[str, str, str]:
        return (_run_id(run) or "", str(run.get("status")), str(run.get("task_id", "")))

    return tuple(sorted(run_shape(run) for run in before.runs)) != tuple(sorted(run_shape(run) for run in after.runs))


def _native_topology_change(before: BoardSnapshot, after: BoardSnapshot) -> bool:
    """Detect changes that cannot be accepted as a normal done lane transition."""
    if any(before.native_task.get(field) != after.native_task.get(field)
           for field in ("route", "dependencies")):
        return True

    def run_shape(run: Mapping[str, Any]) -> tuple[str, str, str]:
        return (_run_id(run) or "", str(run.get("status")), str(run.get("task_id", "")))

    return tuple(sorted(run_shape(run) for run in before.runs)) != tuple(sorted(run_shape(run) for run in after.runs))


def _coherent_done_evidence(task_id: str, snapshot: BoardSnapshot,
                            candidate_evidence: tuple[Mapping[str, Any], ...],
                            review_evidence: tuple[Mapping[str, Any], ...]) -> tuple[bool, bool, bool]:
    """Return coherence and stale candidate/review flags for the current candidate."""
    candidate_id = snapshot.native_task.get("candidate_identity")
    if not isinstance(candidate_id, str) or not candidate_id:
        return False, False, False
    candidate = any(evidence.get("task_id") == task_id and evidence.get("candidate_identity") == candidate_id
                    for evidence in candidate_evidence)
    stale_candidate = any(evidence.get("task_id") == task_id and evidence.get("candidate_identity") != candidate_id
                          for evidence in candidate_evidence)
    matching_review = any(evidence.get("task_id") == task_id and evidence.get("candidate_identity") == candidate_id
                          and evidence.get("verdict") in {"approved", "accepted"} for evidence in review_evidence)
    stale_review = any(evidence.get("task_id") == task_id and evidence.get("candidate_identity") != candidate_id
                       for evidence in review_evidence)
    return candidate and matching_review, stale_candidate, stale_review


def reconcile_operator_edits(scope: Mapping[str, Any], members: tuple[ManagedMember, ...],
                             prior_snapshots: tuple[BoardSnapshot, ...], snapshots: tuple[BoardSnapshot, ...],
                             runs: tuple[Mapping[str, Any], ...], *,
                             candidate_evidence: tuple[Mapping[str, Any], ...] = (),
                             review_evidence: tuple[Mapping[str, Any], ...] = (),
                             pause_intent: PauseIntent | None = None) -> ReconciliationDecision:
    """Classify observed manual changes without restoring or inventing board state."""
    resolved_scope = _scope(scope)
    before_by_task = _snapshots_by_task(prior_snapshots)
    changed: list[str] = []
    done: list[str] = []
    stale_candidate = False
    stale_review = False
    incoherent_done = False
    for member, observed in _member_snapshots(resolved_scope, members, snapshots):
        if observed is None:
            continue
        before = before_by_task.get(member.task_id)
        is_done = observed.native_task.get("state") == "done"
        if is_done:
            done.append(member.task_id)
            coherent, stale_candidate_row, stale_review_row = _coherent_done_evidence(
                member.task_id, observed, candidate_evidence, review_evidence,
            )
            stale_candidate = stale_candidate or stale_candidate_row
            stale_review = stale_review or stale_review_row
            incoherent_done = incoherent_done or not coherent
        if before is not None and (
            _native_topology_change(before, observed)
            or (_native_change(before, observed) and not is_done)
        ):
            changed.append(member.task_id)
    preserved = tuple(sorted(set(changed + done)))
    if stale_candidate:
        return ReconciliationDecision("conflict", (), _report(resolved_scope, "stale_candidate_evidence_invalidated", preserved=preserved, required="inspect_preserved_candidate"))
    if stale_review:
        return ReconciliationDecision("conflict", (), _report(resolved_scope, "stale_review_evidence_invalidated", preserved=preserved, required="inspect_preserved_candidate"))
    if incoherent_done:
        return ReconciliationDecision("conflict", (), _report(resolved_scope, "human_done_edit_requires_evidence_review", preserved=preserved, required="inspect_preserved_candidate"))
    if changed:
        return ReconciliationDecision("conflict", (), _report(resolved_scope, "manual_native_edit_requires_review", preserved=preserved, required="inspect_preserved_candidate"))
    if done:
        return ReconciliationDecision("adopted", (), _report(resolved_scope, "coherent_manual_done_adopted", preserved=preserved))
    containment = verify_containment(resolved_scope, members, snapshots, runs, prior_snapshots=prior_snapshots, pause_intent=pause_intent)
    return ReconciliationDecision(containment.outcome, (), containment.report)


def can_resume(scope: Mapping[str, Any], *, pause_intent: PauseIntent | None = None,
               reconciliation: ReconciliationDecision | None = None, unknown_effects: bool = False,
               budget_exhausted: bool = False, unsafe_human_edits: bool = False,
               operator_authorized_resume: bool = False) -> ResumeDecision:
    """Fail closed; cancellation needs a new explicit operator authorization."""
    _scope(scope)
    if not isinstance(operator_authorized_resume, bool):
        raise ValueError("operator_authorized_resume must be a boolean")
    if pause_intent is not None and pause_intent.cancellation_requested and not operator_authorized_resume:
        return ResumeDecision(False, "cancellation_requires_operator_authorization")
    if pause_intent is not None and pause_intent.origin == "operator":
        return ResumeDecision(False, "operator_pause_persisted")
    if unknown_effects:
        return ResumeDecision(False, "unknown_effects")
    if budget_exhausted:
        return ResumeDecision(False, "budget_exhausted")
    if unsafe_human_edits or (reconciliation is not None and reconciliation.outcome in {"conflict", "partial", "unknown"}):
        return ResumeDecision(False, "unsafe_human_edits")
    return ResumeDecision(True, "coherent")


def reject_late_result(scope: Mapping[str, Any], run_id: str, *, pause_intent: PauseIntent | None = None) -> LateResultDecision:
    """Cancellation invalidates results but deliberately does not delete their work."""
    _scope(scope)
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if pause_intent is not None and pause_intent.cancellation_requested:
        return LateResultDecision(True, "cancellation_requested")
    if pause_intent is not None and pause_intent.origin == "operator":
        return LateResultDecision(True, "operator_pause_persisted")
    return LateResultDecision(False, "eligible")
