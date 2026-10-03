from __future__ import annotations

import copy

import pytest

from tests.fixtures.m4_recovery_review import candidate, completed_review, coordinator, create_and_release, reopened_coordinator, review, snapshot


def test_recover_premature_done_tick_creates_one_held_review_then_releases_once(tmp_path):
    ctl, board, store = coordinator(tmp_path)
    try:
        board.task = snapshot("piece", "done", ({"id": "implement-run", "status": "completed", "outcome": "completed", "profile": "implementer", "metadata": {"worker_session_id": "implementation-session"}},))
        first = ctl.tick()
        second = ctl.tick()
        third = ctl.tick()
        state = store.read_scope(ctl.scope)
        reviews = [member for member in state["members"] if member.role == "local_review"]
        assert first == {"outcome": "held", "actions_attempted": 1, "task_id": "separate-review"}
        assert second == {"outcome": "released", "actions_attempted": 1, "task_id": "separate-review"}
        assert third == {"outcome": "no-op", "actions_attempted": 0}
        assert board.calls == ["create-held", "release"]
        assert len(reviews) == 1 and reviews[0].work_association == "premature-done:piece:content"
    finally:
        store.close()


def test_done_separate_review_without_verdict_gets_one_budgeted_replacement_then_releases(tmp_path):
    ctl, board, store = coordinator(tmp_path)
    try:
        create_and_release(ctl, board)
        board.task = completed_review()
        first = ctl.tick()
        before_restart = store.read_scope(ctl.scope)
        before_members = before_restart["members"]
        before_budget_events = before_restart["budget_events"]
        store.close()
        ctl, store = reopened_coordinator(tmp_path, board)
        second = ctl.tick()
        third = ctl.tick()
        after_release = store.read_scope(ctl.scope)
        replacements = [member for member in after_release["members"] if member.work_association == "premature-done-replacement:separate-review:content"]
        repair_events = [event for event in after_release["budget_events"] if event["event_id"].startswith("workflow_repairs:")]
        assert first == {"outcome": "held", "actions_attempted": 1, "task_id": "replacement-review"}
        assert second == {"outcome": "released", "actions_attempted": 1, "task_id": "replacement-review"}
        assert third == {"outcome": "no-op", "actions_attempted": 0}
        assert board.calls == ["create-held", "release", "create-held", "release"]
        # The initial and replacement creates consume the two finite repair units;
        # fresh-coordinator recovery and repeated polling register neither another
        # member nor another charge.
        assert after_release["members"] == before_members
        assert after_release["budget_events"] == before_budget_events
        assert len(replacements) == 1 and len(repair_events) == 2
        store.close()
        ctl, store = reopened_coordinator(tmp_path, board)
        assert ctl.tick() == {"outcome": "no-op", "actions_attempted": 0}
        final = store.read_scope(ctl.scope)
        assert final["members"] == before_members
        assert final["budget_events"] == before_budget_events
        assert board.calls == ["create-held", "release", "create-held", "release"]
    finally:
        store.close()


@pytest.mark.parametrize(("case", "reason"), (
    ("done_without_claim", "verified_separate_review_provenance_missing"),
    ("archived", "verified_separate_review_provenance_missing"),
    ("malformed_verdict", "review_verdict_invalid"),
    ("missing_evidence", "trusted_git_freeze_unavailable"),
))
def test_nonapproval_inputs_do_not_persist_local_approval(tmp_path, case, reason):
    """Canonical scenario 13: lane state and incomplete evidence never become approval."""
    ctl, board, store = coordinator(tmp_path)
    try:
        create_and_release(ctl, board)
        evidence = review()
        if case == "done_without_claim":
            board.task = completed_review(claimed=False)
        elif case == "archived":
            board.task = completed_review(status="archived")
        elif case == "malformed_verdict":
            board.task = completed_review()
            evidence["verdict"] = "approved-ish"
        else:
            board.task = completed_review()
            evidence = copy.deepcopy(evidence)
            evidence.pop("criterion_evidence")
        before = store.read_scope(ctl.scope)["reviews"]
        result = ctl.submit_review("separate-review", candidate(), evidence, expected_profile="local-review")
        assert result == {"outcome": "held", "reason": reason}
        assert store.read_scope(ctl.scope)["reviews"] == before == ()
    finally:
        store.close()
