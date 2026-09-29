from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from local_first_orchestrator.contracts import Action
from local_first_orchestrator.hermes_board import BoardCapabilities, HermesBoardAdapter

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}


@dataclass
class FakeKanban:
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    create_runs: list[dict[str, Any]] = field(default_factory=list)
    comment_author: str = "local-first-orchestrator"
    comment_body: str | None = None
    after_block: Any = None
    after_link: Any = None
    create_returncode_after_effect: int | None = None
    native_returncode_after_effect: dict[str, int] = field(default_factory=dict)
    expose_reviewer: bool = False
    archived_task_ids: set[str] = field(default_factory=set)

    def add(self, task_id: str, *, status: str = "blocked", runs: list[dict[str, Any]] | None = None,
            title: str | None = None, body: str = "body", assignee: str = "worker", workspace: str = "dir:/repo", archived: bool = False) -> None:
        self.tasks[task_id] = {"task": {"id": task_id, "title": title or task_id, "body": body, "status": "archived" if archived else status,
                                         "assignee": assignee, "workspace": workspace}, "parents": [],
                               "comments": [], "events": [], "runs": [] if runs is None else runs, "attachments": []}
        if archived:
            self.archived_task_ids.add(task_id)

    def __call__(self, argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["env"]["HERMES_HOME"] == "/fixture/home"
        assert kwargs["env"]["HERMES_KANBAN_HOME"] == "/fixture/kanban"
        args = tuple(argv[4:])
        self.calls.append(tuple(argv))
        if args[0] == "show":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks.get(args[1], {})), "")
        if args[0] == "runs":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks.get(args[1], {}).get("runs", [])), "")
        if args[0] == "list":
            include_archived = "--archived" in args
            return subprocess.CompletedProcess(argv, 0, json.dumps([x["task"] for task_id, x in self.tasks.items() if include_archived or task_id not in self.archived_task_ids]), "")
        if args[0] == "create":
            task_id = f"new-{len(self.tasks) + 1}"
            get = lambda name: args[args.index(name) + 1]
            self.add(task_id, title=args[1], body=get("--body"), assignee=get("--assignee"), workspace=get("--workspace"),
                     runs=list(self.create_runs))
            self.tasks[task_id]["task"]["idempotency_key"] = get("--idempotency-key")
            parent = get("--parent")
            self.tasks[task_id]["parents"].append(parent)
            if self.create_returncode_after_effect is not None:
                code = self.create_returncode_after_effect
                self.create_returncode_after_effect = None
                return subprocess.CompletedProcess(argv, code, "", "ambiguous create result")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"id": task_id}), "")
        if args[0] == "comment":
            self.tasks[args[1]]["comments"].append({"author": self.comment_author, "body": self.comment_body or args[2]})
        elif args[0] == "request-review":
            self.tasks[args[1]]["task"]["status"] = "review"
            if self.expose_reviewer and "--reviewer" in args:
                self.tasks[args[1]]["task"]["reviewer"] = args[args.index("--reviewer") + 1]
        elif args[0] == "reopen-review": self.tasks[args[1]]["task"]["status"] = "ready"
        elif args[0] == "block":
            source_status = self.tasks[args[1]]["task"]["status"]
            self.tasks[args[1]]["task"]["status"] = "blocked"
            self.tasks[args[1]]["comments"].append({"author": self.comment_author, "body": f"BLOCKED: {args[2]}"})
            self.tasks[args[1]]["events"].append({"kind": "blocked", "payload": {"reason": args[2], "kind": "needs_input", "recurrences": 1, "source_status": source_status}})
            if self.after_block: self.after_block(self.tasks[args[1]])
        elif args[0] == "unblock":
            self.tasks[args[1]]["task"]["status"] = "ready"
            reason = args[args.index("--reason") + 1]
            self.tasks[args[1]]["comments"].append({"author": "hermes", "body": f"UNBLOCK: {reason}"})
            self.tasks[args[1]]["events"].append({"kind": "unblocked", "payload": None})
        elif args[0] == "link":
            self.tasks[args[2]]["parents"].append(args[1])
            if self.after_link: self.after_link(self.tasks[args[2]])
        elif args[0] == "complete": self.tasks[args[1]]["task"]["status"] = "done"
        else: return subprocess.CompletedProcess(argv, 2, "", "unsupported")
        if args[0] in self.native_returncode_after_effect:
            return subprocess.CompletedProcess(argv, self.native_returncode_after_effect.pop(args[0]), "", "ambiguous native result")
        return subprocess.CompletedProcess(argv, 0, "", "")


def adapter(fake: FakeKanban, tmp_path, **kwargs) -> HermesBoardAdapter:
    executable = tmp_path / "hermes"; executable.write_text("fixture")
    kwargs.setdefault("create_lock_assertion", lambda *_: None)
    kwargs.setdefault("claim_create_attempt", lambda _scope, _key: None)
    return HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-1", executable=str(executable), runner=fake,
                              hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"), timeout_seconds=3, **kwargs)


def action(board: HermesBoardAdapter, key: str, effect: str, target: dict[str, Any], *, identity: str | None = None) -> Action:
    observed_task = target.get("task_id", "anchor-1")
    return Action(key, SCOPE, target, effect, identity or board.read_task(observed_task).digest)


def mutations(fake: FakeKanban) -> list[tuple[str, ...]]:
    return [call for call in fake.calls if call[4] in {"create", "comment", "request-review", "reopen-review", "block", "unblock", "link", "complete"}]


def test_capabilities_expose_only_safe_native_primitives():
    capabilities = BoardCapabilities.native_m0()
    assert capabilities.create_held and capabilities.comment and capabilities.link
    assert not capabilities.exact_run_stop and not capabilities.atomic_read_bound_mutation and not capabilities.complete_anchor


def test_constructor_binds_explicit_anchor():
    with pytest.raises(TypeError):
        HermesBoardAdapter(board="fixture-board", executable="/bin/true")


def test_scoped_run_reader_binds_exact_native_identity(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    fake_runner.add("member-1", runs=[{"id": "run-1", "status": "failed", "started_at": None}])
    board = adapter(fake_runner, tmp_path, managed_member_lookup=lambda scope, task: task == "member-1")
    assert board.read_scoped_run(SCOPE, "member-1", "run-1") == {
        "id": "run-1", "status": "failed", "started_at": None,
        "task_id": "member-1", "board_id": "fixture-board", "anchor_task_id": "anchor-1",
    }
    with pytest.raises(ValueError, match="scope"):
        board.read_scoped_run({"board_id": "fixture-board", "anchor_task_id": "other"}, "member-1", "run-1")
    with pytest.raises(KeyError):
        board.read_scoped_run(SCOPE, "member-1", "missing-run")
    fake_runner.tasks["member-1"]["runs"][0]["task_id"] = "foreign-task"
    with pytest.raises(ValueError, match="contradicts"):
        board.read_scoped_run(SCOPE, "member-1", "run-1")
    blocked = adapter(fake_runner, tmp_path, managed_member_lookup=None)
    with pytest.raises(ValueError, match="trusted managed member"):
        blocked.read_scoped_run(SCOPE, "member-1", "run-1")


def test_stale_or_wrong_anchor_action_conflicts_before_any_write(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    board = adapter(fake_runner, tmp_path)
    stale = action(board, "stale", "hold", {"task_id": "anchor-1"}, identity="sha256:stale")
    assert board.hold(stale, "anchor-1", "pause").outcome == "conflict"
    wrong_anchor = Action("wrong", {"board_id": "fixture-board", "anchor_task_id": "other"}, {"task_id": "anchor-1"}, "hold", board.read_task("anchor-1").digest)
    assert board.hold(wrong_anchor, "anchor-1", "pause").outcome == "conflict"
    assert not mutations(fake_runner)


def test_member_target_requires_injected_trusted_membership_lookup(fake_runner, tmp_path):
    fake_runner.add("anchor-1"); fake_runner.add("member-1", status="ready")
    blocked = adapter(fake_runner, tmp_path)
    result = blocked.hold(action(blocked, "x", "hold", {"task_id": "member-1"}), "member-1", "pause")
    assert result.outcome == "conflict" and not mutations(fake_runner)
    allowed = adapter(fake_runner, tmp_path, managed_member_lookup=lambda scope, task: task == "member-1")
    assert allowed.hold(action(allowed, "y", "hold", {"task_id": "member-1"}), "member-1", "pause").outcome == "verified"


def test_create_held_requires_a_trusted_held_lock_before_any_native_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    board = adapter(fake_runner, tmp_path, create_lock_assertion=None)
    result = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new",
                               body="body <!-- local-first-action:create -->", assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "unsupported"
    assert not [call for call in mutations(fake_runner) if call[4] == "create"]


def test_create_held_requires_a_durable_attempt_claim_before_any_native_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: None, claim_create_attempt=None)
    result = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new",
                               body="body <!-- local-first-action:create -->", assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "unsupported"
    assert not [call for call in mutations(fake_runner) if call[4] == "create"]


def test_create_held_accepts_instance_lock_style_none_return_and_claims_before_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    claims: list[tuple[dict[str, str], str]] = []
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: None,
                    claim_create_attempt=lambda scope, key: claims.append((dict(scope), key)))
    result = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new",
                               body="body <!-- local-first-action:create -->", assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "verified"
    assert claims == [(SCOPE, "create")]
    assert len([call for call in mutations(fake_runner) if call[4] == "create"]) == 1


def test_create_held_pre_reads_anchor_and_verifies_all_supported_identity_fields(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda scope, anchor: scope == SCOPE and anchor == "anchor-1")
    body = "body <!-- local-first-action:create -->"
    result = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new", body=body,
                               assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "verified"
    created = result.readback["native_task"]
    assert created["title"] == "new" and created["body"].startswith(body) and created["assignee"] == "worker"
    assert "<!-- local-first-create:v1:sha256:" in created["body"]
    assert [dict(parent) for parent in result.readback["parents"]] == [{"id": "anchor-1"}]
    call = next(call for call in mutations(fake_runner) if call[4] == "create")
    assert "--parent" in call and call[call.index("--parent") + 1] == "anchor-1"


def test_create_held_with_running_run_is_partial_not_verified(fake_runner, tmp_path):
    fake_runner.add("anchor-1"); fake_runner.create_runs = [{"id": "r", "status": "running"}]
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: True)
    assert board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new", body="body <!-- local-first-action:create -->", assignee="worker", workspace="dir:/repo", idempotency_key="create").outcome == "partial"


def test_comment_marker_is_idempotent_and_requires_author_and_exact_marker(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    board = adapter(fake_runner, tmp_path)
    text = "<!-- local-first-action:comment-1 --> note"
    act = action(board, "comment-1", "comment", {"task_id": "anchor-1"})
    assert board.comment(act, "anchor-1", text).outcome == "verified"
    assert board.comment(action(board, "comment-1", "comment", {"task_id": "anchor-1"}), "anchor-1", text).outcome == "no-op"
    assert len([x for x in mutations(fake_runner) if x[4] == "comment"]) == 1
    fake_runner.comment_author = "other"; fake_runner.comment_body = text
    assert board.comment(action(board, "comment-2", "comment", {"task_id": "anchor-1"}), "anchor-1", "<!-- local-first-action:comment-2 --> note").outcome == "conflict"


def test_hold_detects_running_claim_that_appears_after_pre_read(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="ready")
    fake_runner.after_block = lambda task: task["runs"].append({"id": "race", "status": "running"})
    board = adapter(fake_runner, tmp_path)
    assert board.hold(action(board, "hold", "hold", {"task_id": "anchor-1"}), "anchor-1", "pause").outcome == "partial"


def test_link_requires_trusted_member_and_exact_child_observation(fake_runner, tmp_path):
    fake_runner.add("anchor-1"); fake_runner.add("child-1")
    board = adapter(fake_runner, tmp_path)
    act = Action("link", SCOPE, {"parent_task_id": "anchor-1", "child_task_id": "child-1"}, "link", board.read_task("child-1").digest)
    assert board.link(act, "anchor-1", "child-1").outcome == "conflict"
    trusted = adapter(fake_runner, tmp_path, managed_member_lookup=lambda scope, task: task == "child-1")
    act = Action("link", SCOPE, {"parent_task_id": "anchor-1", "child_task_id": "child-1"}, "link", trusted.read_task("child-1").digest)
    assert trusted.link(act, "anchor-1", "child-1").outcome == "verified"


def test_create_held_reconciles_one_exact_marker_without_a_second_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    body = "body <!-- local-first-action:create -->"
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: True)
    create_action = action(board, "create", "create_held", {"anchor_task_id": "anchor-1"})
    fake_runner.add("existing", title="new", body=f"{body}\n\n{board._create_marker(create_action)}", assignee="worker", workspace="dir:/repo")
    fake_runner.tasks["existing"]["task"]["idempotency_key"] = "create"
    fake_runner.tasks["existing"]["parents"] = ["anchor-1"]
    result = board.create_held(create_action, title="new", body=body,
                               assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "no-op"
    assert not [call for call in mutations(fake_runner) if call[4] == "create"]


def test_create_held_reconciles_archived_marker_as_conflict_without_a_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    body = "body <!-- local-first-action:create -->"
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: None)
    create_action = action(board, "create", "create_held", {"anchor_task_id": "anchor-1"})
    fake_runner.add("archived", title="new", body=f"{body}\n\n{board._create_marker(create_action)}", assignee="worker", workspace="dir:/repo", archived=True)
    fake_runner.tasks["archived"]["task"]["idempotency_key"] = "create"
    fake_runner.tasks["archived"]["parents"] = ["anchor-1"]
    result = board.create_held(create_action, title="new", body=body,
                               assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "conflict"
    assert not [call for call in mutations(fake_runner) if call[4] == "create"]
    assert any(call[4:] == ("list", "--archived", "--json") for call in fake_runner.calls)


def test_create_held_conflicts_on_duplicate_exact_markers_without_a_create(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    body = "body <!-- local-first-action:create -->"
    board = adapter(fake_runner, tmp_path, create_lock_assertion=lambda *_: True)
    create_action = action(board, "create", "create_held", {"anchor_task_id": "anchor-1"})
    for task_id in ("existing-1", "existing-2"):
        fake_runner.add(task_id, title="new", body=f"{body}\n\n{board._create_marker(create_action)}", assignee="worker", workspace="dir:/repo")
        fake_runner.tasks[task_id]["task"]["idempotency_key"] = "create"
        fake_runner.tasks[task_id]["parents"] = ["anchor-1"]
    result = board.create_held(create_action, title="new", body=body,
                               assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert result.outcome == "conflict"
    assert not [call for call in mutations(fake_runner) if call[4] == "create"]


def test_create_held_unknown_result_is_not_retried_and_later_reconciles_by_marker(fake_runner, tmp_path):
    fake_runner.add("anchor-1")
    fake_runner.create_returncode_after_effect = 1
    held = True
    lock_calls = []
    def assert_lock(scope, anchor):
        lock_calls.append((scope, anchor))
        return held and scope == SCOPE and anchor == "anchor-1"
    board = adapter(fake_runner, tmp_path, create_lock_assertion=assert_lock)
    body = "body <!-- local-first-action:create -->"
    first = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new", body=body,
                              assignee="worker", workspace="dir:/repo", idempotency_key="create")
    second = board.create_held(action(board, "create", "create_held", {"anchor_task_id": "anchor-1"}), title="new", body=body,
                               assignee="worker", workspace="dir:/repo", idempotency_key="create")
    assert first.outcome == "unknown" and second.outcome == "no-op"
    assert len([call for call in mutations(fake_runner) if call[4] == "create"]) == 1
    assert len(lock_calls) == 3


def test_link_post_read_is_partial_when_child_starts_running_during_link(fake_runner, tmp_path):
    fake_runner.add("anchor-1"); fake_runner.add("child-1")
    fake_runner.after_link = lambda child: child["runs"].append({"id": "race", "status": "running"})
    board = adapter(fake_runner, tmp_path, managed_member_lookup=lambda scope, task: task == "child-1")
    act = Action("link-race", SCOPE, {"parent_task_id": "anchor-1", "child_task_id": "child-1"}, "link", board.read_task("child-1").digest)
    assert board.link(act, "anchor-1", "child-1").outcome == "partial"


def test_request_review_requires_waiting_lane_and_observed_requested_distinct_reviewer(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="ready", assignee="implementer")
    board = adapter(fake_runner, tmp_path)
    assert board.request_review(action(board, "review", "request_review", {"task_id": "anchor-1"}), "anchor-1", "checks", reviewer="implementer").outcome == "conflict"
    assert board.request_review(action(board, "review", "request_review", {"task_id": "anchor-1"}), "anchor-1", "checks", reviewer="reviewer").outcome == "partial"


def test_complete_anchor_is_unsupported_without_trusted_evidence_and_verified_with_it(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="ready")
    board = adapter(fake_runner, tmp_path)
    act = action(board, "done", "complete_anchor", {"task_id": "anchor-1"})
    assert board.complete_anchor(act, "anchor-1", "prose").outcome == "unsupported"
    trusted = adapter(fake_runner, tmp_path, completion_evidence_verifier=lambda scope, task, evidence: evidence == "accepted")
    assert trusted.complete_anchor(action(trusted, "done", "complete_anchor", {"task_id": "anchor-1"}), "anchor-1", "accepted").outcome == "verified"


def test_verify_effect_never_verifies_only_a_lane(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked")
    board = adapter(fake_runner, tmp_path)
    assert board.verify_effect(action(board, "verify", "hold", {"task_id": "anchor-1"})).outcome == "unsupported"


def test_unknown_native_hold_is_reconciled_read_only_by_exact_scoped_marker(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="ready")
    fake_runner.native_returncode_after_effect["block"] = 1
    board = adapter(fake_runner, tmp_path)
    act = action(board, "recover-hold", "hold", {"task_id": "anchor-1"})
    assert board.hold(act, "anchor-1", "operator prose").outcome == "unknown"
    writes = len(mutations(fake_runner))
    marker = board._native_marker(act)
    assert fake_runner.tasks["anchor-1"]["comments"] == [{"author": fake_runner.comment_author, "body": f"BLOCKED: {marker}"}]
    assert fake_runner.tasks["anchor-1"]["events"] == [{"kind": "blocked", "payload": {"reason": marker, "kind": "needs_input", "recurrences": 1, "source_status": "ready"}}]
    restarted = adapter(fake_runner, tmp_path)
    assert restarted.verify_effect(act).outcome == "verified"
    assert len(mutations(fake_runner)) == writes


def test_unknown_native_release_is_reconciled_read_only_by_exact_scoped_marker(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked")
    fake_runner.native_returncode_after_effect["unblock"] = 1
    board = adapter(fake_runner, tmp_path)
    act = action(board, "recover-release", "release", {"task_id": "anchor-1"})
    assert board.release(act, "anchor-1", "operator prose").outcome == "unknown"
    writes = len(mutations(fake_runner))
    marker = board._native_marker(act)
    assert fake_runner.tasks["anchor-1"]["comments"] == [{"author": "hermes", "body": f"UNBLOCK: {marker}"}]
    assert fake_runner.tasks["anchor-1"]["events"] == [{"kind": "unblocked", "payload": None}]
    restarted = adapter(fake_runner, tmp_path)
    assert restarted.verify_effect(act).outcome == "verified"
    assert len(mutations(fake_runner)) == writes


def test_hold_recovery_requires_marker_and_event_not_only_blocked_lane(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked")
    board = adapter(fake_runner, tmp_path)
    act = action(board, "manual-hold", "hold", {"task_id": "anchor-1"})
    assert board.verify_effect(act).outcome == "unsupported"
    marker = board._native_marker(act)
    fake_runner.tasks["anchor-1"]["comments"].append({"author": "hermes", "body": f"BLOCKED: {marker}"})
    assert board.verify_effect(act).outcome == "unknown"


def test_hold_recovery_with_active_run_is_partial_and_wrong_scope_conflicts(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked", runs=[{"id": "run", "status": "running"}])
    board = adapter(fake_runner, tmp_path)
    act = action(board, "held-running", "hold", {"task_id": "anchor-1"})
    marker = board._native_marker(act)
    fake_runner.tasks["anchor-1"]["comments"].append({"author": "hermes", "body": f"BLOCKED: {marker}"})
    fake_runner.tasks["anchor-1"]["events"].append({"kind": "blocked", "payload": {"reason": marker, "kind": "needs_input", "recurrences": 1, "source_status": "ready"}})
    assert board.verify_effect(act).outcome == "partial"
    wrong_scope = Action("held-running", {"board_id": "fixture-board", "anchor_task_id": "other"}, {"task_id": "anchor-1"}, "hold", act.expected_observed_identity)
    assert board.verify_effect(wrong_scope).outcome == "conflict"


def test_release_recovery_after_dispatcher_claim_is_partial_not_success(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked")
    fake_runner.native_returncode_after_effect["unblock"] = 1
    board = adapter(fake_runner, tmp_path)
    act = action(board, "release-claimed", "release", {"task_id": "anchor-1"})
    assert board.release(act, "anchor-1", "operator prose").outcome == "unknown"
    writes = len(mutations(fake_runner))
    fake_runner.tasks["anchor-1"]["task"]["status"] = "running"
    fake_runner.tasks["anchor-1"]["runs"].append({"id": "dispatcher", "status": "running"})
    assert board.verify_effect(act).outcome == "partial"
    assert len(mutations(fake_runner)) == writes


def test_recovery_rejects_duplicate_or_cross_operation_scoped_markers(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="blocked")
    board = adapter(fake_runner, tmp_path)
    act = action(board, "duplicate-hold", "hold", {"task_id": "anchor-1"})
    marker = board._native_marker(act)
    fake_runner.tasks["anchor-1"]["comments"] = [
        {"author": "hermes", "body": f"BLOCKED: {marker}"},
        {"author": "hermes", "body": f"BLOCKED: {marker}"},
    ]
    fake_runner.tasks["anchor-1"]["events"] = [{"kind": "blocked", "payload": {"reason": marker}}]
    assert board.verify_effect(act).outcome == "conflict"
    fake_runner.tasks["anchor-1"]["comments"] = [{"author": "hermes", "body": f"UNBLOCK: {marker}"}]
    assert board.verify_effect(act).outcome == "conflict"


def test_create_markers_are_scoped_to_board_anchor_and_action_key(fake_runner, tmp_path):
    fake_runner.add("anchor-1"); fake_runner.add("anchor-2")
    first = adapter(fake_runner, tmp_path)
    executable = tmp_path / "hermes-two"; executable.write_text("fixture")
    second = HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-2", executable=str(executable), runner=fake_runner,
                                hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"),
                                create_lock_assertion=lambda *_: None, claim_create_attempt=lambda *_: None)
    first_action = action(first, "same-key", "create_held", {"anchor_task_id": "anchor-1"})
    second_action = Action("same-key", {"board_id": "fixture-board", "anchor_task_id": "anchor-2"}, {"anchor_task_id": "anchor-2"}, "create_held", second.read_task("anchor-2").digest)
    first_result = first.create_held(first_action, title="one", body="body", assignee="worker", workspace="dir:/repo", idempotency_key="same-key")
    second_result = second.create_held(second_action, title="two", body="body", assignee="worker", workspace="dir:/repo", idempotency_key="same-key")
    assert first_result.outcome == second_result.outcome == "verified"
    assert first_result.readback["native_task"]["body"] != second_result.readback["native_task"]["body"]
    assert first.create_held(first_action, title="one", body="body", assignee="worker", workspace="dir:/repo", idempotency_key="same-key").outcome == "no-op"
    assert second.create_held(second_action, title="two", body="body", assignee="worker", workspace="dir:/repo", idempotency_key="same-key").outcome == "no-op"
    assert len([call for call in mutations(fake_runner) if call[4] == "create"]) == 2
    assert all("<!-- local-first-create:" in task["task"]["body"] for task in fake_runner.tasks.values() if task["task"]["id"].startswith("new-"))


def test_every_native_mutation_requires_lock_immediately_before_cli_write(fake_runner, tmp_path):
    fake_runner.add("anchor-1", status="ready", assignee="implementer"); fake_runner.add("child-1")
    board = adapter(fake_runner, tmp_path, create_lock_assertion=None,
                    managed_member_lookup=lambda _scope, task: task == "child-1",
                    completion_evidence_verifier=lambda *_: True)
    calls_before = len(mutations(fake_runner))
    assert board.hold(action(board, "hold", "hold", {"task_id": "anchor-1"}), "anchor-1", "pause").outcome == "unsupported"
    assert board.request_review(action(board, "review", "request_review", {"task_id": "anchor-1"}), "anchor-1", "checks", reviewer="reviewer").outcome == "unsupported"
    fake_runner.tasks["anchor-1"]["task"]["status"] = "review"
    assert board.return_waiting_review(action(board, "return", "return_waiting_review", {"task_id": "anchor-1"}), "anchor-1", "reason").outcome == "unsupported"
    fake_runner.tasks["anchor-1"]["task"]["status"] = "blocked"
    assert board.release(action(board, "release", "release", {"task_id": "anchor-1"}), "anchor-1", "reason").outcome == "unsupported"
    assert board.comment(action(board, "comment", "comment", {"task_id": "anchor-1"}), "anchor-1", "<!-- local-first-action:comment --> note").outcome == "unsupported"
    link_action = Action("link", SCOPE, {"parent_task_id": "anchor-1", "child_task_id": "child-1"}, "link", board.read_task("child-1").digest)
    assert board.link(link_action, "anchor-1", "child-1").outcome == "unsupported"
    fake_runner.tasks["anchor-1"]["task"]["status"] = "ready"
    assert board.complete_anchor(action(board, "done", "complete_anchor", {"task_id": "anchor-1"}), "anchor-1", "accepted").outcome == "unsupported"
    assert len(mutations(fake_runner)) == calls_before


@pytest.fixture
def fake_runner(): return FakeKanban()
