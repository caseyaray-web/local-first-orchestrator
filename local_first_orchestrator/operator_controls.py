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
_TERMINAL_RUN_STATUSES = frozenset({"cancelled", "completed", "done", "stopped"})
_INERT_RUN_STATUSES = _TERMINAL_RUN_STATUSES | frozenset({"blocked"})
_SUPPORTED_TASK_STATUSES = frozenset({"blocked", "ready", "todo", "review", "running", "done", "archived"})
_HELD_TASK_STATUSES = frozenset({"blocked"})
_TERMINAL_TASK_STATUSES = frozenset({"done", "archived"})


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
    intent: PauseIntent | None = None


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


def _managed_task_ids(scope: Mapping[str, str], members: tuple[ManagedMember, ...]) -> tuple[str, ...]:
    """Return the immutable in-scope membership the pause must account for."""
    return tuple(sorted({member.task_id for member in members
                         if member.board_id == scope["board_id"] and member.anchor_task_id == scope["anchor_task_id"]}))


def _run_id(run: Mapping[str, Any]) -> str | None:
    value = run.get("id")
    return value if isinstance(value, str) and value else None


def _run_is_live(run: Mapping[str, Any]) -> bool:
    return run.get("status") in _ACTIVE_RUN_STATUSES


def _task_status(snapshot: BoardSnapshot) -> str | None:
    """Return only a native Hermes task status that this policy understands."""
    status = snapshot.native_task.get("status")
    return status if status in _SUPPORTED_TASK_STATUSES else None


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


def _uncontained_observed_runs(prior_snapshots: tuple[BoardSnapshot, ...],
                                current_snapshots: tuple[BoardSnapshot, ...],
                                effect_results: tuple[ActionResult, ...]) -> tuple[str, ...]:
    """Describe live runs that have not received an exact verified stop receipt.

    A later empty/terminal runs list is only a current observation.  It cannot
    prove that a cancellation request stopped an earlier worker, particularly
    when an authorized parent harness performed its own owned-PID cleanup.
    Preserve the original task/run/PID observation in a partial report instead
    of letting an empty current list look like containment proof.
    """
    current = _snapshots_by_task(current_snapshots)
    uncontained: list[str] = []
    for before in prior_snapshots:
        task_id = before.native_task.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        after = current.get(task_id)
        for run in before.runs:
            run_id = _run_id(run)
            if run_id is None or not _run_is_live(run):
                continue
            stopped = after is not None and _stopped_in_snapshot(after, run_id)
            verified = any(_stop_result_matches(result, task_id, run_id)
                           for result in effect_results)
            if stopped and verified:
                continue
            pid = run.get("worker_pid")
            pid_text = str(pid) if type(pid) is int and pid > 0 else "unknown"
            uncontained.append(f"{task_id}/{run_id}/pid:{pid_text}")
    return tuple(sorted(set(uncontained)))


def _runs_have_known_statuses(snapshots: tuple[BoardSnapshot, ...]) -> bool:
    """Do not clear a pause while any observed native run has an unknown state.

    A historical blocked run is inert evidence, not proof that a requested stop
    succeeded; stop verification remains limited to terminal statuses below.
    """
    return all(
        isinstance(run.get("status"), str) and run["status"] in _ACTIVE_RUN_STATUSES | _INERT_RUN_STATUSES
        for snapshot in snapshots for run in snapshot.runs
    )


def _snapshots_by_task(snapshots: tuple[BoardSnapshot, ...]) -> dict[str, BoardSnapshot]:
    return {
        task_id: snapshot
        for snapshot in snapshots
        if isinstance((task_id := snapshot.native_task.get("id")), str) and task_id
    }


def expected_native_digest(pause_intent: PauseIntent, task_id: str, *,
                           verified_applied_digests: Mapping[str, str] | None = None) -> str | None:
    """Return the only digest a restart may accept without silently adopting edits.

    A coordinator may replace a pause baseline only with a digest from a verified
    applied operation readback.  Unverified or absent effects leave the persisted
    observation baseline authoritative for diagnostics, not for board mutation.
    """
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty string")
    if verified_applied_digests is None:
        verified_applied_digests = {}
    if not isinstance(verified_applied_digests, Mapping):
        raise ValueError("verified_applied_digests must be a mapping")
    replacement = verified_applied_digests.get(task_id)
    if replacement is not None and (not isinstance(replacement, str) or not replacement):
        raise ValueError("verified applied digests must be non-empty strings")
    return replacement if replacement is not None else pause_intent.baseline_digests.get(task_id)


def native_digest_mismatches(scope: Mapping[str, Any], members: tuple[ManagedMember, ...],
                             snapshots: tuple[BoardSnapshot, ...], pause_intent: PauseIntent, *,
                             verified_applied_digests: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Identify restart-time native observations that cannot be silently adopted."""
    resolved_scope = _scope(scope)
    current = _snapshots_by_task(snapshots)
    mismatches = []
    for member, _ in _member_snapshots(resolved_scope, members, snapshots):
        expected = expected_native_digest(
            pause_intent, member.task_id, verified_applied_digests=verified_applied_digests,
        )
        observed = current.get(member.task_id)
        if expected is None or observed is None or observed.digest != expected:
            mismatches.append(member.task_id)
    return tuple(sorted(set(mismatches)))


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
        and readback.get("status") in _TERMINAL_RUN_STATUSES
    )


def _stopped_in_snapshot(snapshot: BoardSnapshot, run_id: str) -> bool:
    return any(_run_id(run) == run_id and run.get("status") in _TERMINAL_RUN_STATUSES for run in snapshot.runs)


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
    baseline_digests = {
        member.task_id: observed.digest
        for member, observed in _member_snapshots(resolved_scope, members, snapshots)
        if observed is not None
    }
    intent = PauseIntent(resolved_scope, "operator", generation, stop, cancellation,
                         active=True, managed_task_ids=_managed_task_ids(resolved_scope, members),
                         baseline_digests=baseline_digests)
    actions: list[Action] = []
    active: list[str] = []
    partial = False
    for member, observed in _member_snapshots(resolved_scope, members, snapshots):
        if observed is None:
            partial = True
            continue
        live = _active_runs((observed,))
        active.extend(live)
        status = _task_status(observed)
        if live:
            partial = True
            if stop:
                by_id = {str(run.get("id")): run for run in observed.runs}
                for run_id in live:
                    action = plan_stop({**dict(by_id[run_id]), "task_id": member.task_id}, resolved_scope,
                                       expected_observed_identity=observed.digest)
                    if action is not None:
                        actions.append(action)
            elif status == "running":
                # A hold may lose the independent-dispatch race; it is still the
                # only supported proposed containment, with partial reported.
                actions.append(Action(f"hold:{member.task_id}:{observed.digest}:pause:{generation}", resolved_scope,
                                      {"task_id": member.task_id}, "hold", observed.digest))
        elif status not in _HELD_TASK_STATUSES and status not in _TERMINAL_TASK_STATUSES:
            actions.append(Action(f"hold:{member.task_id}:{observed.digest}:pause:{generation}", resolved_scope,
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
        status = _task_status(observed)
        if status not in _HELD_TASK_STATUSES and status not in _TERMINAL_TASK_STATUSES:
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
        # ``active_workers`` is containment telemetry, not a claim that the
        # listed PID is still live at the final read.  Keep both currently-live
        # IDs and earlier live task/run/PID observations lacking a matching
        # verified stop receipt; a blank final runs list is not containment.
        uncontained = tuple(sorted(set(active) | set(
            _uncontained_observed_runs(prior_snapshots, snapshots, effect_results)
        )))
        return ContainmentVerification("partial", _report(resolved_scope, "containment_unverified", active=uncontained, required="inspect_exact_task_and_run_ids"))
    return ContainmentVerification("verified", _report(resolved_scope, "containment_verified"))


def _native_change(before: BoardSnapshot, after: BoardSnapshot) -> bool:
    """Compare only native control fields, not observation metadata or digest."""
    for field in ("status", "lane", "route", "dependencies"):
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
        is_done = _task_status(observed) == "done"
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


def _release_result_matches(result: ActionResult, task_id: str, action_key: str,
                            current: BoardSnapshot) -> bool:
    """Accept one exact persisted release only with adapter-native readback."""
    readback = result.readback
    if result.outcome != "verified" or result.action_key != action_key or readback is None:
        return False
    try:
        if set(readback) != set(BoardSnapshot.__dataclass_fields__):
            return False
        # ActionResult freezes transport lists into tuples, while the adapter's
        # actual evidence remains a complete BoardSnapshot-shaped mapping.
        verified = BoardSnapshot(**dict(readback))
    except (TypeError, ValueError):
        return False
    return (
        verified.native_task.get("id") == task_id
        and _task_status(verified) in {"ready", "todo"}
        and verified.digest == current.digest
        and _runs_have_known_statuses((verified,))
        and not _active_runs((verified,))
    )


def _resume_releases_verified(scope: Mapping[str, str], pause_intent: PauseIntent,
                              members: tuple[ManagedMember, ...], snapshots: tuple[BoardSnapshot, ...],
                              effect_results: tuple[ActionResult, ...]) -> bool:
    if any(result.outcome in {"ambiguous", "unknown", "partial"} for result in effect_results):
        return False
    current = _snapshots_by_task(snapshots)
    scoped = _managed_task_ids(scope, members)
    if (
        not scoped or tuple(sorted(pause_intent.managed_task_ids)) != scoped
        or set(pause_intent.baseline_digests) != set(scoped)
        or set(pause_intent.resuming_task_ids) != set(scoped)
        or set(pause_intent.resuming_action_keys) != set(scoped)
    ):
        return False
    for task_id in pause_intent.resuming_task_ids:
        observed = current.get(task_id)
        action_key = pause_intent.resuming_action_keys.get(task_id)
        if (
            task_id not in scoped or observed is None or action_key is None
            or _task_status(observed) not in {"ready", "todo"}
            or not _runs_have_known_statuses((observed,))
        ):
            return False
        if not any(_release_result_matches(result, task_id, action_key, observed) for result in effect_results):
            return False
    return _runs_have_known_statuses(snapshots) and not _active_runs(snapshots)


def can_resume(scope: Mapping[str, Any], *, pause_intent: PauseIntent | None = None,
               reconciliation: ReconciliationDecision | None = None, unknown_effects: bool = False,
               budget_exhausted: bool = False, unsafe_human_edits: bool = False,
               operator_authorized_resume: bool = False,
               members: tuple[ManagedMember, ...] = (), snapshots: tuple[BoardSnapshot, ...] = (),
               effect_results: tuple[ActionResult, ...] = ()) -> ResumeDecision:
    """Fail closed and require release proof before a resuming intent can clear."""
    resolved_scope = _scope(scope)
    if not isinstance(operator_authorized_resume, bool):
        raise ValueError("operator_authorized_resume must be a boolean")
    if unknown_effects:
        return ResumeDecision(False, "unknown_effects")
    if budget_exhausted:
        return ResumeDecision(False, "budget_exhausted")
    if unsafe_human_edits or (reconciliation is not None and reconciliation.outcome in {"conflict", "partial", "unknown"}):
        return ResumeDecision(False, "unsafe_human_edits")
    if pause_intent is not None and pause_intent.active:
        if pause_intent.cancellation_requested and not operator_authorized_resume:
            return ResumeDecision(False, "cancellation_requires_operator_authorization")
        if not operator_authorized_resume:
            if pause_intent.origin == "operator":
                return ResumeDecision(False, "operator_pause_persisted")
            return ResumeDecision(False, "pause_requires_operator_authorization")
        if reconciliation is None:
            return ResumeDecision(False, "reconciliation_required")
        if pause_intent.managed_task_ids and not members:
            return ResumeDecision(False, "resume_member_observations_incomplete")
        if pause_intent.resuming:
            if not _resume_releases_verified(resolved_scope, pause_intent, members, snapshots, effect_results):
                return ResumeDecision(False, "resume_release_unverified")
        elif members:
            scoped = _managed_task_ids(resolved_scope, members)
            observed = _snapshots_by_task(snapshots)
            if (
                not scoped
                or tuple(sorted(pause_intent.managed_task_ids)) != scoped
                or set(pause_intent.baseline_digests) != set(scoped)
                or any(observed.get(task_id) is None for task_id in scoped)
            ):
                return ResumeDecision(False, "resume_member_observations_incomplete")
            releases: list[Action] = []
            for member, observed in _member_snapshots(resolved_scope, members, snapshots):
                if observed is not None and _task_status(observed) in _HELD_TASK_STATUSES:
                    # Digest alone can recur after a later pause; the new durable
                    # resuming generation prevents an old applied release receipt
                    # from being mistaken for this explicit clear.
                    releases.append(Action(
                        f"release:{member.task_id}:{observed.digest}:resume:{pause_intent.generation + 1}",
                        resolved_scope, {"task_id": member.task_id}, "release", observed.digest,
                    ))
            release_ids = tuple(action.target["task_id"] for action in releases)
            release_keys = {str(action.target["task_id"]): action.key for action in releases}
            if not release_ids or set(release_ids) != set(scoped):
                return ResumeDecision(False, "resume_release_unverified")
            return ResumeDecision(
                True, "coherent", tuple(releases),
                PauseIntent(pause_intent.scope, "operator", pause_intent.generation + 1, False, False,
                            active=True, managed_task_ids=pause_intent.managed_task_ids,
                            baseline_digests=pause_intent.baseline_digests,
                            resuming=True, resuming_task_ids=release_ids, resuming_action_keys=release_keys),
            )
    return ResumeDecision(True, "coherent")


def plan_resume(scope: Mapping[str, Any], *, pause_intent: PauseIntent | None = None,
                reconciliation: ReconciliationDecision | None = None, unknown_effects: bool = False,
                budget_exhausted: bool = False, unsafe_human_edits: bool = False,
                operator_authorized_resume: bool = False,
                members: tuple[ManagedMember, ...] = (), snapshots: tuple[BoardSnapshot, ...] = (),
                effect_results: tuple[ActionResult, ...] = ()) -> ResumeDecision:
    """Enter a durable release phase, then clear only after exact release proofs.

    Callers that omit members retain the legacy single-clear transport behavior;
    coordinators handling managed members must pass their snapshots and persist the
    returned resuming intent before applying its release actions.
    """
    decision = can_resume(
        scope, pause_intent=pause_intent, reconciliation=reconciliation,
        unknown_effects=unknown_effects, budget_exhausted=budget_exhausted,
        unsafe_human_edits=unsafe_human_edits, operator_authorized_resume=operator_authorized_resume,
        members=members, snapshots=snapshots, effect_results=effect_results,
    )
    if not decision.allowed:
        return decision
    if pause_intent is None or not pause_intent.active:
        return ResumeDecision(False, "no_active_pause_to_clear")
    if decision.intent is not None:
        return decision
    return ResumeDecision(
        True, decision.reason, decision.actions,
        PauseIntent(pause_intent.scope, "operator", pause_intent.generation + 1, False, False,
                    active=False, managed_task_ids=pause_intent.managed_task_ids,
                    baseline_digests=pause_intent.baseline_digests),
    )


def reject_late_result(scope: Mapping[str, Any], run_id: str, *, pause_intent: PauseIntent | None = None,
                       run_generation: int | None = None) -> LateResultDecision:
    """Cancellation invalidates results but deliberately does not delete their work."""
    _scope(scope)
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if run_generation is not None and (not isinstance(run_generation, int) or isinstance(run_generation, bool) or run_generation < 0):
        raise ValueError("run_generation must be a non-negative integer or None")
    if pause_intent is not None and pause_intent.active and pause_intent.cancellation_requested:
        return LateResultDecision(True, "cancellation_requested")
    if pause_intent is not None and pause_intent.active and pause_intent.resuming:
        return LateResultDecision(True, "resuming_release_in_progress")
    if pause_intent is not None and pause_intent.active and pause_intent.origin == "operator":
        return LateResultDecision(True, "operator_pause_persisted")
    if pause_intent is not None and not pause_intent.active:
        if run_generation is None:
            return LateResultDecision(True, "run_provenance_required")
        if run_generation != pause_intent.generation:
            return LateResultDecision(True, "pre_clear_generation")
    return LateResultDecision(False, "eligible")
