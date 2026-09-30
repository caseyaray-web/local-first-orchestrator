import json
import subprocess

import pytest

from local_first_orchestrator.contracts import Action
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.planning_coordinator import request_payload
from tests.test_hermes_board_adapter import FakeKanban
from tests.test_m4_plan_evidence import evidence, request


def contract():
    req = request()
    identity = req.identity
    key = "planner-op"
    workspace = "/repo"
    profile = "planner"
    target = {
        "task_id": req.anchor_id, "anchor_task_id": req.anchor_id,
        "request": request_payload(req), "request_identity": identity,
        "association": identity, "native_parent": False,
        "planner_marker": {"kind": "planner_card", "request_identity": identity,
                         "workspace": workspace, "profile": profile, "operation_key": key},
        "reviewer_profile": profile, "create_title": "Planning: " + identity[:48],
        "create_body": json.dumps({"request_identity": identity, "marker": {
            "kind": "planner_card", "request_identity": identity, "workspace": workspace,
            "profile": profile, "operation_key": key}}, sort_keys=True), "create_workspace": "dir:" + workspace,
        "create_idempotency_key": key,
    }
    return Action(key, {"board_id": req.board_id, "anchor_task_id": req.anchor_id}, target, "create_held", "observed"), req


def valid(action):
    return HermesBoardAdapter._valid_parentless_planner(action, assignee="planner", workspace="dir:/repo", idempotency_key="planner-op")


def test_valid_parentless_planner_contract():
    assert valid(contract()[0])


@pytest.mark.parametrize("request_id", ["request-1", "x" * 256])
def test_optional_request_id_contract_accepts_string_or_absence(request_id):
    action, _ = contract()
    target = dict(action.target)
    target["request_id"] = request_id
    assert valid(Action(action.key, action.scope, target, action.effect, action.expected_observed_identity))


@pytest.mark.parametrize("request_id", [None, "", 1, True, [], {}, "x" * 257, "\ud800"])
def test_optional_request_id_contract_rejects_malformed_present_values(request_id):
    action, _ = contract()
    target = dict(action.target)
    target["request_id"] = request_id
    assert not valid(Action(action.key, action.scope, target, action.effect, action.expected_observed_identity))


def test_optional_request_id_contract_accepts_absence():
    assert valid(contract()[0])


@pytest.mark.parametrize("change", [
    lambda t: t.pop("request"),
    lambda t: t.pop("association"),
    lambda t: t.update(request={**t["request"], "board_id": "foreign"}),
    lambda t: t.update(request_identity="wrong"),
    lambda t: t.update(request={**t["request"], "profile": "wrong"}),
    lambda t: t["planner_marker"].update(workspace="wrong"),
    lambda t: t["planner_marker"].update(operation_key="wrong"),
    lambda t: t.update(candidate={}),
    lambda t: t.update(association="foreign"),
    lambda t: t.update(create_workspace="/repo"),
    lambda t: t.update(create_workspace="worktree:/repo"),
    lambda t: t.update(unexpected_target_field=True),
    lambda t: t.update(create_workspace="dir:/repo/../repo"),
    lambda t: t.update(create_workspace="dir:/repo//child"),
])
def test_invalid_parentless_planner_contract(change):
    action, _ = contract()
    target = dict(action.target)
    target["planner_marker"] = dict(action.target["planner_marker"])
    change(target)
    altered = Action(action.key, action.scope, target, action.effect, action.expected_observed_identity)
    assert not valid(altered)


def test_malformed_planner_create_conflicts_before_native_cli(tmp_path):
    fake_runner = FakeKanban()
    fake_runner.add("anchor-1")
    executable = tmp_path / "hermes"
    executable.write_text("fixture")
    board = HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-1", executable=str(executable),
        runner=fake_runner, hermes_home=tmp_path, kanban_home=tmp_path,
        create_lock_assertion=lambda *_: None, claim_create_attempt=lambda *_: None)
    action, _ = contract()
    target = dict(action.target)
    target["planner_marker"] = {**target["planner_marker"], "operation_key": "wrong"}
    bad = Action("planner-op", {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}, target, "create_held", "unused")
    result = board.create_held(bad, title="Planning", body="body", assignee="planner", workspace="/repo", idempotency_key="planner-op")
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake_runner.calls)


@pytest.mark.parametrize("field", ["title", "body"])
def test_planner_create_arguments_must_match_durable_action_before_cli(tmp_path, field):
    fake_runner = FakeKanban()
    fake_runner.add("anchor-1")
    executable = tmp_path / "hermes"
    executable.write_text("fixture")
    board = HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-1", executable=str(executable),
        runner=fake_runner, hermes_home=tmp_path, kanban_home=tmp_path,
        create_lock_assertion=lambda *_: None, claim_create_attempt=lambda *_: None)
    action, _ = contract()
    args = {"title": action.target["create_title"], "body": action.target["create_body"]}
    args[field] += " arbitrary"
    result = board.create_held(action, title=args["title"], body=args["body"], assignee="planner",
        workspace="dir:/repo", idempotency_key="planner-op")
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake_runner.calls)
