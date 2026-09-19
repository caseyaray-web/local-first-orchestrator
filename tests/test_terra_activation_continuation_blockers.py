from __future__ import annotations

import copy

import pytest

from local_first_orchestrator.native_release_approval import validate_activation_post_snapshot


def _snapshot(*, comments=None, events=None, status="scheduled"):
    return {
        "task": {"id": "T", "status": status, "title": "x", "body": "b",
                  "workspace_path": None, "assignee": None, "workspace_kind": None,
                  "session_id": None, "branch_name": "main", "started_at": None,
                  "completed_at": None},
        "comments": [] if comments is None else comments,
        "events": [] if events is None else events,
        "parents": [],
        "children": [],
        "latest_summary": None,
        "runs": [],

    }


def _exact_pair(marker="m", *, timestamp=20):
    pre = _snapshot()
    post = copy.deepcopy(pre)
    post["task"]["status"] = "ready"
    post["comments"] = [{"author": "operator", "body": f"UNBLOCK: {marker}", "created_at": timestamp}]
    post["events"] = [{"run_id": None, "kind": "unblocked", "payload": None, "created_at": timestamp + 1}]
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

    for field, value in (("comments", {"body": "UNBLOCK: m"}), ("events", {"kind": "unblocked", "payload": None})):
        _, post = _exact_pair()
        post[field].append(value)
        with pytest.raises(ValueError):
            validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")




def _continuation_pair(status="running"):
    pre = _snapshot(status="ready")
    pre["task"].update(assignee="worker-code-local", workspace_kind="worktree", workspace_path="/repo/.worktrees/T", repository_identity="/repo", base_sha="base", branch_name="wt/T")
    post = copy.deepcopy(pre)
    post["task"]["status"] = status
    run = {"id": 7, "status": status, "outcome": None if status == "running" else "blocked", "started_at": 30, "ended_at": None if status == "running" else 40, "summary": None if status == "running" else "local-first-awaiting-reconciliation", "profile": "worker-code-local", "worker_pid": 1234 if status == "running" else None, "metadata": None}
    post["runs"].append(run)
    post["task"]["started_at"] = 30
    post["task"]["session_id"] = "session-T" if status == "running" else None
    post["events"].append({"kind": "claimed", "payload": {"lock": "lock-T", "expires": 90, "run_id": 7}, "created_at": 30, "run_id": 7})
    if status == "blocked":
        post["latest_summary"] = "local-first-awaiting-reconciliation"
        post["events"].append({"kind": "blocked", "payload": {"reason": "local-first-awaiting-reconciliation"}, "created_at": 40, "run_id": 7})
    return pre, post


def test_running_continuation_is_authorized_but_not_terminal():
    from local_first_orchestrator.native_release_approval import validate_activation_continuation_snapshot
    pre, post = _continuation_pair()
    assert validate_activation_continuation_snapshot(pre, post, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation") == "running"
    post["runs"][0]["worker_pid"] = None
    with pytest.raises(ValueError):
        validate_activation_continuation_snapshot(pre, post, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation")


def test_canonical_snapshot_preserves_unknown_root_and_nested_fields():
    from local_first_orchestrator.native_release_approval import canonical_snapshot_json
    snapshot = _snapshot()
    snapshot["latest_summary"] = {"future": ["value", {"nested": True}]}
    snapshot["task"]["future_task_field"] = {"x": 1}
    snapshot["runs"].append({"id": 1, "status": "done", "started_at": 1, "future_run_field": [3, 2, 1]})
    first = canonical_snapshot_json(snapshot)
    snapshot["latest_summary"]["future"].append("changed")
    assert first != canonical_snapshot_json(snapshot)


def test_continuation_rejects_unknown_field_drift_everywhere():
    from local_first_orchestrator.native_release_approval import validate_activation_continuation_snapshot
    pre, post = _continuation_pair()
    for location in ("root", "task", "run", "event"):
        before, after = copy.deepcopy(pre), copy.deepcopy(post)
        if location == "root":
            before["future"] = "same"; after["future"] = "changed"
        elif location == "task":
            before["task"]["future"] = "same"; after["task"]["future"] = "changed"
        elif location == "run":
            before["runs"].append({"id": 99, "status": "old", "started_at": 1, "future": "same"})
            after["runs"].append({"id": 99, "status": "old", "started_at": 1, "future": "changed"})
        else:
            before["events"].append({"kind": "old", "future": "same"})
            after["events"].append({"kind": "old", "future": "changed"})
        with pytest.raises(ValueError):
            validate_activation_continuation_snapshot(before, after, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation")


def test_terminal_handoff_uses_observed_running_session_lineage():
    from local_first_orchestrator.native_release_approval import validate_activation_continuation_snapshot
    activation_post, _ = _continuation_pair()
    _, terminal = _continuation_pair("blocked")
    _, running = _continuation_pair()
    from local_first_orchestrator.native_release_approval import canonical_snapshot_json
    observation = {"run_id": 7, "session_id": "session-T", "pid": 1234, "snapshot_json": canonical_snapshot_json(running)}
    observation["snapshot_hash"] = __import__("hashlib").sha256(observation["snapshot_json"].encode()).hexdigest()
    observation.update(profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T")
    assert validate_activation_continuation_snapshot(activation_post, terminal, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation", prior_running_observation=observation) == "terminal"
    observation["run_id"] = 8
    with pytest.raises(ValueError):
        validate_activation_continuation_snapshot(activation_post, terminal, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation", prior_running_observation=observation)

def test_terminal_handoff_accepts_hermes_triage_normalization_with_same_lineage():
    from local_first_orchestrator.native_release_approval import canonical_snapshot_json, validate_activation_continuation_snapshot
    activation_post, _ = _continuation_pair()
    _, terminal = _continuation_pair("blocked")
    _, running = _continuation_pair()
    terminal["task"]["status"] = "triage"
    observation = {"run_id": 7, "session_id": "session-T", "pid": 1234, "snapshot_json": canonical_snapshot_json(running)}
    observation["snapshot_hash"] = __import__("hashlib").sha256(observation["snapshot_json"].encode()).hexdigest()
    observation.update(profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T")
    assert validate_activation_continuation_snapshot(activation_post, terminal, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation", prior_running_observation=observation) == "terminal"
    with pytest.raises(ValueError, match="prior running observation"):
        validate_activation_continuation_snapshot(activation_post, terminal, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation")



def test_direct_terminal_handoff_without_observed_running_is_rejected():
    from local_first_orchestrator.native_release_approval import validate_activation_continuation_snapshot
    pre, post = _continuation_pair("blocked")
    with pytest.raises(ValueError, match="prior running observation"):
        validate_activation_continuation_snapshot(pre, post, acknowledged_at=20, profile="worker-code-local", workspace_path="/repo/.worktrees/T", branch="wt/T", repository_identity="/repo", base_sha="base", handoff_summary="local-first-awaiting-reconciliation")


def test_activation_rejects_reordered_or_altered_histories():
    pre, post = _exact_pair()
    post["comments"] = [{"id": 2, "task_id": "T", "author": "operator", "body": "prefix UNBLOCK: m", "created_at": 20}]
    with pytest.raises(ValueError):
        validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")

    pre, post = _exact_pair()
    post["events"][0]["payload"] = {"marker": "m"}
    with pytest.raises(ValueError):
        validate_activation_post_snapshot(pre, post, marker_present=True, marker="m")
