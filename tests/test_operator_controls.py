from __future__ import annotations

import pytest

from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, ManagedMember, PauseIntent
from local_first_orchestrator.operator_controls import (
    can_resume,
    plan_pause,
    plan_stop,
    reconcile_operator_edits,
    reject_late_result,
    verify_containment,
)

SCOPE = {"board_id": "board-1", "anchor_task_id": "anchor-1"}


def member(task_id: str) -> ManagedMember:
    return ManagedMember("board-1", "anchor-1", task_id, "implementation", 0, ("finding-1",), f"work-{task_id}")


def snapshot(task_id: str, state: str, *runs: dict[str, str], **native_task: object) -> BoardSnapshot:
    return BoardSnapshot(
        native_task={"id": task_id, "state": state, **native_task}, parents=(), runs=tuple(runs),
        comments=(), events=(), attachments=(), observed_at="2026-09-28T00:00:00Z",
        digest=f"digest-{task_id}-{state}",
    )


@pytest.mark.parametrize(
    ("name", "task_state", "runs", "stop", "outcome", "effects", "active"),
    [
        ("queued", "queued", (), False, "pending", ("hold",), ()),
        ("claim race", "claimed", ({"id": "run-race", "status": "running"},), False, "partial", ("hold",), ("run-race",)),
        ("active run", "running", ({"id": "run-active", "status": "running"},), True, "partial", ("stop_run",), ("run-active",)),
        ("partial stop", "running", ({"id": "run-live", "status": "running", "stop_supported": "false"},), True, "partial", (), ("run-live",)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pause_planning_is_scoped_and_honest_about_dispatch_races(
    name, task_state, runs, stop, outcome, effects, active,
):
    planned = plan_pause(SCOPE, stop, generation=4, members=(member("task-1"),),
                         snapshots=(snapshot("task-1", task_state, *runs),))

    assert planned.intent == PauseIntent(SCOPE, "operator", 4, stop, False)
    assert planned.outcome == outcome
    assert tuple(action.effect for action in planned.actions) == effects
    assert planned.report.active_workers == active
    assert {action.target.get("task_id") for action in planned.actions} <= {"task-1"}


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ({"id": "run-1", "task_id": "task-1", "status": "running", "observed_identity": "digest-1"}, "stop_run"),
        ({"id": "run-1", "task_id": "task-1", "status": "running", "stop_supported": "false", "observed_identity": "digest-1"}, None),
        ({"id": "run-1", "status": "running", "observed_identity": "digest-1"}, None),
    ],
)
def test_stop_planning_requires_a_live_exact_supported_run(run, expected):
    action = plan_stop({**run, "scope": SCOPE})
    assert (action.effect if action else None) == expected


def test_containment_requires_observed_hold_and_exact_run_stop_before_success():
    prior_held = snapshot("task-1", "held")
    prior = snapshot("task-2", "running", {"id": "run-stopped", "status": "running"})
    queued = snapshot("task-1", "held")
    stopped = snapshot("task-2", "held", {"id": "run-stopped", "status": "stopped"})
    report = verify_containment(
        SCOPE, (member("task-1"), member("task-2")), (queued, stopped),
        ({"id": "run-stopped", "task_id": "task-2", "status": "stopped"},),
        prior_snapshots=(prior_held, prior),
        pause_intent=PauseIntent(SCOPE, "operator", 4, True, False),
        effect_results=(ActionResult(
            "stop_run:task-2:run-stopped:digest-task-2-running", "verified", "exited",
            {"task_id": "task-2", "run_id": "run-stopped", "stop_supported": True, "process_exited": True, "status": "stopped"},
        ),),
    )

    assert report.outcome == "verified"
    assert report.report.active_workers == ()


def test_reconcile_preserves_done_human_edit_and_blocks_unsafe_resume():
    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (), (snapshot("task-1", "done"),), (),
        pause_intent=PauseIntent(SCOPE, "operator", 5, False, False),
    )

    assert result.outcome == "conflict"
    assert result.actions == ()
    assert result.report.preserved_work == ("task-1",)
    decision = can_resume(SCOPE, pause_intent=PauseIntent(SCOPE, "operator", 5, False, False),
                          reconciliation=result)
    assert not decision.allowed
    assert decision.reason == "operator_pause_persisted"


def test_resume_allows_a_coherent_automatic_hold_but_not_an_operator_pause():
    decision = can_resume(SCOPE, pause_intent=PauseIntent(SCOPE, "automatic", 1, False, False))
    assert decision.allowed
    assert decision.reason == "coherent"


def test_automatic_cancellation_never_auto_resumes_and_late_results_stay_rejected():
    cancelled = PauseIntent(SCOPE, "automatic", 2, True, True)

    decision = can_resume(SCOPE, pause_intent=cancelled)
    late = reject_late_result(SCOPE, "run-late", pause_intent=cancelled)

    assert not decision.allowed
    assert decision.reason == "cancellation_requires_operator_authorization"
    authorized = can_resume(SCOPE, pause_intent=cancelled, operator_authorized_resume=True)
    assert authorized.allowed
    assert authorized.reason == "coherent"
    assert late.rejected
    assert late.reason == "cancellation_requested"


@pytest.mark.parametrize(
    ("prior", "current", "effects"),
    [
        ((), (snapshot("task-1", "held"),), ()),
        ((snapshot("task-1", "running", {"id": "run-1", "status": "running"}),),
         (snapshot("task-1", "held", {"id": "run-1", "status": "stopped"}),),
         (ActionResult("stop_run:task-1:other-run:digest", "verified", "exited",
                       {"task_id": "task-1", "run_id": "other-run", "stop_supported": True, "process_exited": True, "status": "stopped"}),)),
    ],
    ids=("held_snapshot_without_captured_run", "mismatched_stop_readback"),
)
def test_stop_containment_does_not_infer_success_without_matching_prior_run_and_exit_proof(prior, current, effects):
    report = verify_containment(
        SCOPE, (member("task-1"),), current, (), prior_snapshots=prior,
        pause_intent=PauseIntent(SCOPE, "operator", 4, True, False), effect_results=effects,
    )

    assert report.outcome == "partial"
    assert report.report.required_operator_action == "inspect_exact_task_and_run_ids"


@pytest.mark.parametrize(
    ("changed"),
    [
        {"state": "blocked"},
        {"route": "manual-route"},
        {"dependencies": ("manual-dependency",)},
        {"runs": ({"id": "manual-run", "status": "running"},)},
    ],
    ids=("blocked", "route", "dependency", "run"),
)
def test_reconcile_reports_manual_native_changes_without_claiming_them_verified(changed):
    before = snapshot("task-1", "ready", route="managed-route", dependencies=("dep-1",))
    native = {key: value for key, value in changed.items() if key != "runs"}
    after = snapshot("task-1", native.pop("state", "ready"), *changed.get("runs", ()),
                     route=native.get("route", "managed-route"),
                     dependencies=native.get("dependencies", ("dep-1",)))

    result = reconcile_operator_edits(SCOPE, (member("task-1"),), (before,), (after,), ())

    assert result.outcome == "conflict"
    assert result.actions == ()
    assert result.report.problem == "manual_native_edit_requires_review"
    assert result.report.required_operator_action == "inspect_preserved_candidate"


def test_reconcile_adopts_manual_done_only_with_matching_candidate_and_review_evidence():
    before = snapshot("task-1", "ready", candidate_identity="candidate-1")
    after = snapshot("task-1", "done", candidate_identity="candidate-1")

    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (before,), (after,), (),
        candidate_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1"},),
        review_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1", "verdict": "approved"},),
    )

    assert result.outcome == "adopted"
    assert result.report.problem == "coherent_manual_done_adopted"
    assert result.report.preserved_work == ("task-1",)


def test_reconcile_rejects_done_that_changes_managed_route_or_dependencies():
    before = snapshot(
        "task-1", "ready", candidate_identity="candidate-1",
        route="managed-route", dependencies=("dep-a",),
    )
    after = snapshot(
        "task-1", "done", candidate_identity="candidate-1",
        route="human-route", dependencies=("dep-b",),
    )

    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (before,), (after,), (),
        candidate_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1"},),
        review_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1", "verdict": "approved"},),
    )

    assert result.outcome == "conflict"
    assert result.actions == ()
    assert result.report.problem == "manual_native_edit_requires_review"
    assert result.report.preserved_work == ("task-1",)
    assert result.report.required_operator_action == "inspect_preserved_candidate"


def test_reconcile_adopts_done_with_harmless_lane_transition_and_exact_evidence():
    before = snapshot(
        "task-1", "ready", candidate_identity="candidate-1",
        lane="implementation", route="managed-route", dependencies=("dep-a",),
    )
    after = snapshot(
        "task-1", "done", candidate_identity="candidate-1",
        lane="completed", route="managed-route", dependencies=("dep-a",),
    )

    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (before,), (after,), (),
        candidate_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1"},),
        review_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1", "verdict": "approved"},),
    )

    assert result.outcome == "adopted"
    assert result.actions == ()
    assert result.report.problem == "coherent_manual_done_adopted"
    assert result.report.preserved_work == ("task-1",)


def test_reconcile_invalidates_stale_review_evidence_without_discarding_manual_done_work():
    before = snapshot("task-1", "ready", candidate_identity="candidate-1")
    after = snapshot("task-1", "done", candidate_identity="candidate-2")

    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (before,), (after,), (),
        candidate_evidence=({"task_id": "task-1", "candidate_identity": "candidate-2"},),
        review_evidence=({"task_id": "task-1", "candidate_identity": "candidate-1", "verdict": "approved"},),
    )

    assert result.outcome == "conflict"
    assert result.actions == ()
    assert result.report.problem == "stale_review_evidence_invalidated"
    assert result.report.preserved_work == ("task-1",)
    assert result.report.required_operator_action == "inspect_preserved_candidate"


def test_reconcile_invalidates_stale_candidate_evidence_alongside_current_evidence():
    before = snapshot("task-1", "ready", candidate_identity="candidate-2")
    after = snapshot("task-1", "done", candidate_identity="candidate-2")

    result = reconcile_operator_edits(
        SCOPE, (member("task-1"),), (before,), (after,), (),
        candidate_evidence=(
            {"task_id": "task-1", "candidate_identity": "candidate-1"},
            {"task_id": "task-1", "candidate_identity": "candidate-2"},
        ),
        review_evidence=({"task_id": "task-1", "candidate_identity": "candidate-2", "verdict": "approved"},),
    )

    assert result.outcome == "conflict"
    assert result.actions == ()
    assert result.report.problem == "stale_candidate_evidence_invalidated"
    assert result.report.preserved_work == ("task-1",)
    assert result.report.required_operator_action == "inspect_preserved_candidate"


@pytest.mark.parametrize(
    ("reason", "kwargs"),
    [
        ("unknown_effects", {"unknown_effects": True}),
        ("budget_exhausted", {"budget_exhausted": True}),
        ("unsafe_human_edits", {"unsafe_human_edits": True}),
    ],
)
def test_resume_rejects_other_unsafe_conditions(reason, kwargs):
    decision = can_resume(SCOPE, **kwargs)
    assert not decision.allowed
    assert decision.reason == reason


def test_cancellation_rejects_late_result_without_discarding_work():
    decision = reject_late_result(
        SCOPE, "run-late", pause_intent=PauseIntent(SCOPE, "operator", 6, True, True),
    )

    assert decision.rejected
    assert decision.reason == "cancellation_requested"
    assert decision.action is None
