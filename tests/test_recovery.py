from dataclasses import asdict

from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember
from local_first_orchestrator.recovery import (
    detect_issues,
    propose_repair,
    summarize_escalation,
    verify_repair,
)

SCOPE = {"board_id": "board-1", "anchor_task_id": "anchor-1"}


def snapshot(*, task_id="impl-1", status="done", runs=(), parents=(), events=()):
    return BoardSnapshot(
        native_task={"id": task_id, "board_id": "board-1", "status": status},
        parents=parents,
        runs=runs,
        comments=(),
        events=events,
        attachments=(),
        observed_at="2026-09-28T12:00:00Z",
        digest="snapshot-1",
    )


def candidate(task_id="impl-1", run_id="run-1", head="a" * 40):
    return CandidateIdentity("repo-1", f"/worktrees/{task_id}", "b" * 40, head, "content-1", "diff-1", run_id, "contract-1")


def review_record(task_id="review-1", candidate_value=None, *, verdict="pass", checks="checks-1", provenance="native-review-run-1", criteria=("criterion-1",)):
    candidate_value = candidate() if candidate_value is None else candidate_value
    return {
        "task_id": task_id,
        "verdict": verdict,
        "candidate": candidate_value.to_dict(),
        "check_identity": checks,
        "native_review_provenance": provenance,
        "criterion_evidence": criteria,
    }


def evidence(*, members=(), candidates=(), reviews=(), operations=(), budgets=()):
    return {"scope": SCOPE, "members": members, "candidates": candidates, "reviews": reviews,
            "operations": operations, "budget_events": budgets, "operator_intent": None}


def member(task_id="impl-1", role="implementation", generation=0, work="piece-1"):
    return ManagedMember("board-1", "anchor-1", task_id, role, generation, (), work)


def kinds(found):
    return {issue.kind for issue in found}


def test_accidental_done_requires_separate_review_of_exact_preserved_candidate():
    found = detect_issues(snapshot(), evidence(members=(member(),), candidates=(candidate(),)), {"head_sha": "a" * 40})
    issue = next(issue for issue in found if issue.kind == "missing_local_review")

    action = propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}})

    assert issue.task_id == "impl-1" and issue.run_id == "run-1"
    assert action.effect == "create_review"
    assert action.target["candidate"]["head_sha"] == "a" * 40


def test_done_review_without_usable_verdict_gets_bounded_replacement_review():
    found = detect_issues(snapshot(task_id="review-1"), evidence(
        members=(member("review-1", "local_review"),), candidates=(candidate(),),
        reviews=({"task_id": "review-1", "candidate_head": "a" * 40, "verdict": "malformed"},),
    ), {"head_sha": "a" * 40})
    issue = next(issue for issue in found if issue.kind == "missing_review_verdict")

    assert propose_repair(issue, {"workflow_recovery": {"limit": 1, "used": 0}}).effect == "create_replacement_review"


def test_stale_head_invalidates_approval_without_inferring_acceptance_from_done_lane():
    found = detect_issues(snapshot(), evidence(
        members=(member(),), candidates=(candidate(),),
        reviews=(review_record(),),
    ), {"head_sha": "c" * 40})

    assert "stale_approval" in kinds(found)
    assert propose_repair(next(i for i in found if i.kind == "stale_approval"), {"workflow_recovery": {"limit": 2, "used": 0}}).effect == "create_review"


def test_unknown_create_is_held_for_identity_reconciliation_not_replayed():
    found = detect_issues(snapshot(status="ready"), evidence(operations=({"key": "create-1", "effect": "create_held", "phase": "unknown", "target": {"task_id": "maybe-1"}},)), {})
    issue = next(issue for issue in found if issue.kind == "unknown_create")

    action = propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}})
    assert action.effect == "reconcile_operation"
    assert "create" not in action.effect


def test_unmatched_candidate_is_never_reused_for_another_task_and_is_held():
    found = detect_issues(snapshot(), evidence(members=(member(),), candidates=(candidate("other-1", "other-run"),)), {})
    issue = next(issue for issue in found if issue.kind == "missing_candidate")

    action = propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}})
    assert issue.candidate is None
    assert action.effect == "hold"


def test_forged_done_review_pass_without_bound_identity_provenance_checks_or_criteria_is_replaced():
    found = detect_issues(snapshot(task_id="review-1"), evidence(
        members=(member("review-1", "local_review"),), candidates=(candidate(),),
        reviews=({"task_id": "review-1", "candidate_head": "a" * 40, "verdict": "pass"},),
    ), {"head_sha": "a" * 40})
    issue = next(issue for issue in found if issue.kind == "missing_review_verdict")

    assert propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}}).effect == "create_replacement_review"


def test_changed_candidate_identity_produces_a_distinct_recovery_finding():
    first = next(issue for issue in detect_issues(snapshot(), evidence(members=(member(),), candidates=(candidate(head="a" * 40),)), {}) if issue.kind == "missing_local_review")
    second = next(issue for issue in detect_issues(snapshot(), evidence(members=(member(),), candidates=(candidate(head="c" * 40),)), {}) if issue.kind == "missing_local_review")

    assert first.finding_id != second.finding_id
    assert first.candidate["head_sha"] != second.candidate["head_sha"]
    assert first.finding_id != second.finding_id


def test_unknown_comment_and_link_are_reconciled_with_marker_and_exact_target_not_resent():
    operations = (
        {"key": "comment-1", "effect": "comment", "phase": "unknown", "marker": "comment-marker", "target": {"task_id": "impl-1", "comment_id": "comment-1"}},
        {"key": "link-1", "effect": "link", "phase": "unknown", "marker": "link-marker", "target": {"task_id": "impl-1", "parent_id": "parent-1"}},
    )
    found = detect_issues(snapshot(status="ready"), evidence(operations=operations), {})
    issues = [issue for issue in found if issue.kind == "unknown_operation"]

    assert len(issues) == 2
    actions = [propose_repair(issue, {"workflow_recovery": {"limit": 3, "used": 0}}) for issue in issues]
    assert {action.effect for action in actions} == {"reconcile_operation"}
    assert {action.target["marker"] for action in actions} == {"comment-marker", "link-marker"}
    assert {action.target["operation_target"]["task_id"] for action in actions} == {"impl-1"}


def test_containment_issue_is_ordered_before_review_correction_for_same_observation():
    found = detect_issues(snapshot(status="running", runs=({"id": "run-1", "status": "running"},), parents=({"id": "parent-1", "accepted": False},)), evidence(operations=({"key": "link-1", "effect": "link", "phase": "unknown", "target": {"task_id": "impl-1"}},)), {})

    assert [issue.kind for issue in found][:2] == ["dependent_early_running", "unknown_operation"]


def test_duplicate_unstarted_cards_hold_redundant_card_but_preserve_both_when_work_exists():
    found = detect_issues(snapshot(status="ready"), evidence(members=(
        member("impl-1", generation=1), member("impl-2", generation=1),
    )), {})
    duplicate = next(issue for issue in found if issue.kind == "duplicate_unstarted")
    assert propose_repair(duplicate, {"workflow_recovery": {"limit": 2, "used": 0}}).effect == "hold"

    worked = detect_issues(snapshot(status="ready"), evidence(members=(
        member("impl-1", generation=1), member("impl-2", generation=1),
    ), candidates=(candidate("impl-2"),)), {})
    assert "duplicate_useful_work" in kinds(worked)
    assert propose_repair(next(i for i in worked if i.kind == "duplicate_useful_work"), {"workflow_recovery": {"limit": 2, "used": 0}}) is None


def test_dependent_running_before_unaccepted_parent_is_contained_before_correction():
    found = detect_issues(snapshot(task_id="child-1", status="running", runs=({"id": "run-child", "status": "running"},), parents=({"id": "impl-1", "accepted": False},)), evidence(), {})
    issue = next(issue for issue in found if issue.kind == "dependent_early_running")

    assert propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}}).effect == "stop_or_park"


def test_human_edit_is_adopted_and_preserves_existing_work():
    found = detect_issues(snapshot(events=({"id": "event-1", "actor": "human", "kind": "board_edit", "task_id": "impl-1"},)), evidence(members=(member(),), candidates=(candidate(),)), {})
    issue = next(issue for issue in found if issue.kind == "human_board_edit")

    action = propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}})
    assert action.effect == "adopt_human_edit"
    assert action.target["preserve_task_ids"] == ("impl-1",)


def test_budget_exhaustion_returns_one_actionable_escalation_and_no_action():
    found = detect_issues(snapshot(), evidence(members=(member(),), candidates=(candidate(),)), {"head_sha": "a" * 40})
    issue = next(issue for issue in found if issue.kind == "missing_local_review")

    assert propose_repair(issue, {"workflow_recovery": {"limit": 1, "used": 1}}) is None
    report = summarize_escalation(SCOPE, (issue,), attempted_actions=("inspect",))
    assert report.required_operator_action and "budget" in report.required_operator_action


def test_classification_and_proposals_do_not_mutate_fixture_inputs_or_execute_effects():
    board = snapshot()
    state = evidence(members=(member(),), candidates=(candidate(),))
    git = {"head_sha": "a" * 40}
    before = (board.to_dict(), repr(state), dict(git))

    issue = next(i for i in detect_issues(board, state, git) if i.kind == "missing_local_review")
    proposed = propose_repair(issue, {"workflow_recovery": {"limit": 2, "used": 0}})

    assert proposed.effect == "create_review"
    assert (board.to_dict(), repr(state), git) == before
    assert verify_repair(ActionResult(proposed.key, "verified", "read back", {"id": "review-1"}))
    assert not verify_repair(ActionResult(proposed.key, "ambiguous", "unknown", None))
