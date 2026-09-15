from __future__ import annotations

import copy

import pytest

from local_first_orchestrator.native_release_approval import validate_activation_post_snapshot


def _snapshot(*, comments=None, events=None, status="scheduled"):
    return {
        "task": {"id": "T", "status": status, "title": "x", "body": "b"},
        "comments": [] if comments is None else comments,
        "task_events": [] if events is None else events,
        "runs": [],
        "session_id": None,
        "branch_name": "main",
        "started_at": None,
        "completed_at": None,
        "current_run_id": None,
    }


def _exact_pair(marker="m", *, timestamp=20):
    pre = _snapshot()
    post = copy.deepcopy(pre)
    post["task"]["status"] = "ready"
    post["comments"] = [{"author": "operator", "body": f"UNBLOCK: {marker}", "created_at": timestamp}]
    post["task_events"] = [{"run_id": None, "kind": "unblocked", "payload": None, "created_at": timestamp + 1}]
    return pre, post


def test_activation_requires_exact_comment_and_unblocked_event():
    pre, post = _exact_pair()
    validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")


def test_activation_rejects_ready_only_and_partial_or_duplicate_effects():
    pre = _snapshot()
    ready_only = copy.deepcopy(pre)
    ready_only["task"]["status"] = "ready"
    with pytest.raises(ValueError):
        validate_activation_post_snapshot(pre, ready_only, marker_present=False, marker="m")

    for field, value in (("comments", {"body": "UNBLOCK: m"}), ("task_events", {"kind": "unblocked", "payload": None})):
        _, post = _exact_pair()
        post[field].append(value)
        with pytest.raises(ValueError):
            validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")




def _continuation_pair(status="running"):
    pre = _snapshot(status="ready")
    pre["task"].update(assignee="worker-code-local", workspace_kind="worktree", workspace_path="/repo/.worktrees/T", repository_identity="/repo", base_sha="base")
    pre["branch_name"] = "wt/T"
    post = copy.deepcopy(pre)
    post["task"]["status"] = status
    run = {"id": 7, "status": status, "outcome": None if status == "running" else "blocked", "started_at": 30, "ended_at": None if status == "running" else 40, "summary": None if status == "running" else "local-first-awaiting-reconciliation", "profile": "worker-code-local", "worker_pid": 1234 if status == "running" else None, "metadata": None}
    post["runs"].append(run)
    post["started_at"] = 30
    post["session_id"] = "session-T" if status == "running" else None
    post["current_run_id"] = 7 if status == "running" else None
    post["task_events"].append({"kind": "claimed", "payload": {"lock": "lock-T", "expires": 90, "run_id": 7}, "created_at": 30, "run_id": 7})
    if status == "blocked":
        post["task_events"].append({"kind": "blocked", "payload": {"reason": "local-first-awaiting-reconciliation"}, "created_at": 40, "run_id": 7})
    return pre, post


def test_running_continuation_is_authorized_but_not_terminal():
    from local_first_orchestrator.native_release_approval import validate_activation_continuation_snapshot
    pre, post = _continuation_pair()
    assert validate_activation_continuation_snapshot(pre, post, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation") == "running"
    post["runs"][0]["worker_pid"] = None
    with pytest.raises(ValueError):
        validate_activation_continuation_snapshot(pre, post, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation")


def test_activation_rejects_reordered_or_altered_histories():
    pre, post = _exact_pair()
    post["comments"] = [{"id": 2, "task_id": "T", "author": "operator", "body": "prefix UNBLOCK: m", "created_at": 20}]
    with pytest.raises(ValueError):
        validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")

    pre, post = _exact_pair()
    post["task_events"][0]["payload"] = {"marker": "m"}
    with pytest.raises(ValueError):
        validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")
