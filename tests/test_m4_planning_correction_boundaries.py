"""Regression boundaries for planner anchors and frozen v1 accepted-piece recovery."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import types

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.decomposition_planner import request_payload
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.planning_coordinator import (
    ActiveTrancheRoute,
    accepted_active_tranche_create_payload,
    first_active_tranche_materialization,
)
from tests.test_m4_piece_adapter import AcceptedDescription, adapter, make_action
from tests.test_m4_plan_acceptance import _pure_acceptance_fixture
from tests.test_m4_plan_evidence import request
from tests.test_m4_planner_anchor_prerequisite import SCOPE, Board, enroll_and_hold, marker, snapshot
from tests.test_hermes_board_adapter import FakeKanban


def _public_controller(tmp_path, current):
    store = EvidenceStore.open(tmp_path / "public.sqlite", create_new=True)
    store.migrate()
    req = request()
    observed = dataclasses.replace(req, board_id="board-A", anchor_id="anchor-A")
    board = Board(current)
    ctl = Coordinator(
        SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "public.lock"),
        budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
        configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner"},
        planning_observer=lambda got: {"request": request_payload(observed)},
        planning_profile="planner", planning_workspace=str(tmp_path),
    )
    return ctl, store, board


def _scope_rows(store):
    state = store.read_scope(SCOPE)
    return (tuple(state["members"]), tuple(state["operations"]), tuple(state["budget_events"]))


def _instrument(store, board, monkeypatch):
    calls = []
    for name in ("reserve_operation", "reserve_paid_release", "record_budget_event"):
        original = getattr(store, name)
        def wrapped(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(store, name, wrapped)
    for name in ("create_held", "release"):
        def forbidden(*_args, _name=name, **_kwargs):
            calls.append("board:" + _name)
            raise AssertionError("public prerequisite failure must not mutate native board")
        monkeypatch.setattr(board, name, forbidden, raising=False)
    return calls


@pytest.mark.parametrize("entrypoint", ["prepare_planner", "release_planner"])
@pytest.mark.parametrize("case", [
    "missing", "ambiguous", "wrong-root", "forged-store-ack", "ready", "live", "stale", "same-digest-drift", "duplicate-current",
])
def test_public_planner_entrypoints_refuse_untrusted_anchor_before_any_store_or_board_effect(tmp_path, monkeypatch, entrypoint, case):
    current = snapshot(marker=marker("current"))
    if case == "forged-store-ack":
        current = snapshot()
    elif case == "ready":
        current = snapshot(status="ready", marker=marker("current"))
    elif case == "live":
        current = snapshot(marker=marker("current"), runs=({"id": "run-A", "status": "running"},))
    elif case == "stale":
        current = snapshot(digest="fresh", marker=marker("current"))
    elif case == "same-digest-drift":
        current = dataclasses.replace(current, native_task={"id": "anchor-A", "status": "blocked", "body": "same digest but changed content"})
    elif case == "duplicate-current":
        current = dataclasses.replace(current,
            comments=({"body": "BLOCKED: " + marker("current")}, {"body": "BLOCKED: " + marker("current")}),
            events=({"kind": "blocked", "payload": {"reason": marker("current"), "kind": "needs_input", "source_status": "ready", "recurrences": 1}},
                    {"kind": "blocked", "payload": {"reason": marker("current"), "kind": "needs_input", "source_status": "ready", "recurrences": 1}}))
    ctl, store, board = _public_controller(tmp_path, current)
    try:
        if case not in {"missing", "wrong-root"}:
            proof = (snapshot() if case == "forged-store-ack" else
                     snapshot(digest="old" if case == "stale" else "anchor-digest", marker=marker("current")))
            enroll_and_hold(store, proof, key="current")
        if case == "ambiguous":
            store.register_member(ManagedMember("board-A", "anchor-A", "other", "root", 1, (), "other-root"))
        elif case == "wrong-root":
            store.register_member(ManagedMember("board-A", "anchor-A", "other", "root", 0, (), "native-root"))

        before = _scope_rows(store)
        calls = _instrument(store, board, monkeypatch)
        with pytest.raises(ValueError, match="enrolled and held|held anchor"):
            getattr(ctl, entrypoint)(request_id="boundary-request")
        assert _scope_rows(store) == before
        assert calls == []
    finally:
        store.close()


@pytest.mark.parametrize("entrypoint", ["prepare_planner", "release_planner"])
def test_public_planner_entrypoints_accept_current_hold_with_terminal_run_and_old_history(tmp_path, monkeypatch, entrypoint):
    current = snapshot(marker=marker("current"), runs=({"id": 1, "status": "blocked", "ended_at": 123},))
    ctl, store, board = _public_controller(tmp_path, current)
    try:
        enroll_and_hold(store, snapshot(digest="old", marker=marker("old")), key="old")
        enroll_and_hold(store, current, key="current")
        if entrypoint == "prepare_planner":
            calls = []
            def stop_at_create(*args, **kwargs):
                calls.append(args[0])
                raise RuntimeError("valid anchor reached managed create boundary")
            monkeypatch.setattr(board, "create_held", stop_at_create, raising=False)
            with pytest.raises(RuntimeError, match="valid anchor reached"):
                ctl.prepare_planner(request_id="boundary-request")
            assert len(calls) == 1
            assert len([op for op in store.read_scope(SCOPE)["operations"] if op.effect == "create_held"]) == 1
        else:
            before = _scope_rows(store)
            with pytest.raises(ValueError, match="exact held planner creation proof"):
                ctl.release_planner(request_id="boundary-request")
            assert _scope_rows(store) == before
    finally:
        store.close()


def _prior_module():
    source = subprocess.check_output([
        "git", "show", "a0405df5ed24c2b58352d0eb2be4c866a6d6db7b:local_first_orchestrator/planning_coordinator.py",
    ], text=True)
    name = "local_first_orchestrator._planning_coordinator_prior_a0405df"
    module = types.ModuleType(name)
    module.__file__ = "<git:a0405df:planning_coordinator.py>"
    module.__package__ = "local_first_orchestrator"
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def _plain(value):
    if hasattr(value, "items"):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _v1_artifact():
    prior = _prior_module()
    current_piece = __import__("tests.test_m4_piece_adapter", fromlist=["payload"]).payload()
    evidence = current_piece.plan_evidence
    token = json.loads(json.dumps(current_piece.target["source_hashes"]))
    # Rebuild the full accepted token from the public fixture, rather than deriving
    # a self-confirming artifact from the current builder.
    body = json.loads(current_piece.body)
    accepted = body["accepted_token"]
    route = accepted["route"]
    material = prior.first_active_tranche_materialization(evidence, prior.ActiveTrancheRoute(**route))
    built = prior.accepted_active_tranche_create_payload(accepted, evidence, material.targets[0])
    return prior, evidence, accepted, material.targets[0], built


def test_current_v1_builder_is_byte_exact_with_independently_loaded_prior_head_artifact():
    prior, evidence, accepted, old_target, old_built = _v1_artifact()
    current_target = first_active_tranche_materialization(evidence, ActiveTrancheRoute(**accepted["route"])).targets[0]
    current = accepted_active_tranche_create_payload(accepted, evidence, current_target, body_kind="accepted_active_tranche_piece_v1")
    assert current.title == old_built.title
    assert current.body == old_built.body
    assert _plain(current.target) == _plain(old_built.target)
    assert current.idempotency_key == old_built.idempotency_key
    assert type(old_target) is prior.HeldCardTarget


def _frozen_receipt(board, piece, task_id="old-piece"):
    action = make_action(piece)
    marker_text = board._create_marker(action)
    task = {"id": task_id, "title": piece.title, "body": piece.body + "\n\n" + marker_text,
            "assignee": piece.target["assignee"], "workspace": piece.target["workspace"], "status": "blocked"}
    data = {"native_task": task, "parents": [], "runs": [], "comments": [], "events": [], "attachments": []}
    snapshot = BoardSnapshot(native_task=task, parents=(), runs=(), comments=(), events=(), attachments=(),
        observed_at="old", digest=board._digest(data))
    show = {"task": dict(task), "parents": [], "children": [], "runs": [],
            "comments": [], "events": [], "latest_summary": None}
    return {**snapshot.to_dict(), "raw_capture_v1": {
        "kind": "hermes_kanban_raw_capture_v1", "show": show, "runs": []}}


def test_production_frozen_receipt_validator_accepts_prior_v1(tmp_path):
    _prior, evidence, _accepted, _target, built = _v1_artifact()
    piece = AcceptedDescription(built.title, built.body, _plain(built.target), built.idempotency_key, evidence)
    fake = FakeKanban(); fake.add("anchor-1")
    board = adapter(tmp_path, fake, lambda *_: piece)
    action = make_action(piece)
    assert board.validate_accepted_piece_frozen_receipt(action, _frozen_receipt(board, piece), "old-piece")["native_task_id"] == "old-piece"


def test_production_frozen_receipt_validator_rejects_v2_root_semantics_tampering(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1")
    v2 = __import__("tests.test_m4_piece_adapter", fromlist=["payload"]).payload()
    body = json.loads(v2.body)
    assert "root_semantics" in body
    body["root_semantics"]["objective"] = "tampered"
    changed = dataclasses.replace(v2, body=json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False), target={**v2.target, "body": json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)})
    bad = adapter(tmp_path, fake, lambda *_: changed)
    with pytest.raises(ValueError):
        bad.validate_accepted_piece_frozen_receipt(make_action(changed), _frozen_receipt(bad, changed), "old-piece")


def test_prepare_active_piece_recovers_applied_prior_v1_intent_without_rewriting_ack(tmp_path):
    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        token = ctl.accept_validated_plan("plan-1")
        prior = _prior_module()
        evidence = store.read_plan(SCOPE, "plan-1")
        old_target = prior.first_active_tranche_materialization(evidence, prior.ActiveTrancheRoute(**token["route"])).targets[0]
        old = prior.accepted_active_tranche_create_payload(_plain(token), evidence, old_target)
        target = {**_plain(old.target), "task_id": "anchor-A", "anchor_task_id": "anchor-A", "native_parent": False,
                  "native_deps": [], "plan_id": "plan-1", "ticket_id": old_target.ticket_id,
                  "accepted_token_key": old.target["accepted_token_key"]}
        old_piece = AcceptedDescription(old.title, old.body, _plain(old.target), old.idempotency_key, _plain(evidence))
        old_board = adapter(tmp_path, FakeKanban(), lambda *_: old_piece)
        intent = OperationIntent(old.idempotency_key, SCOPE, target, "create_held", "active-digest", {}, "verified",
            _frozen_receipt(old_board, old_piece), {}, "applied")
        store.reserve_operation(intent)
        receipt_data = _plain(intent.readback)
        normalized_receipt_data = {key: value for key, value in receipt_data.items() if key != "raw_capture_v1"}
        receipt = BoardSnapshot.from_dict(normalized_receipt_data)
        board.read_task = lambda task_id: BoardSnapshot.from_dict(normalized_receipt_data) if task_id == "old-piece" else BoardSnapshot(
            native_task={"id": "anchor-A", "status": "blocked"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="active-digest")
        board.verify_effect = lambda action: ActionResult(action.key, "no-op", "historical v1 receipt reconciled", intent.readback)
        before = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == old.idempotency_key)
        result = ctl.prepare_active_piece("plan-1", old_target.ticket_id)
        assert result["outcome"] == "held" and result["actions_attempted"] == 0
        after = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == old.idempotency_key)
        assert after == before
        assert receipt.native_task["id"] == result["task_id"]
    finally:
        store.close()


def test_prepare_active_piece_unknown_v2_reuses_native_action_without_coordinator_plan_id(tmp_path):
    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        token = ctl.accept_validated_plan("plan-1")
        evidence = store.read_plan(SCOPE, "plan-1")
        target0 = first_active_tranche_materialization(evidence, ActiveTrancheRoute(**token["route"])).targets[0]
        built = accepted_active_tranche_create_payload(_plain(token), evidence, target0)
        target = {**_plain(built.target), "task_id": "anchor-A", "anchor_task_id": "anchor-A", "native_parent": False,
                  "native_deps": [], "plan_id": "plan-1", "ticket_id": target0.ticket_id,
                  "accepted_token_key": built.target["accepted_token_key"]}
        piece = AcceptedDescription(built.title, built.body, _plain(built.target), built.idempotency_key, _plain(evidence))
        fake = FakeKanban(); fake.add("anchor-1")
        native = adapter(tmp_path, fake, lambda *_: piece)
        receipt = _frozen_receipt(native, piece)
        intent = OperationIntent(built.idempotency_key, SCOPE, target, "create_held", "active-digest", {}, "ambiguous", receipt, {}, "unknown")
        store.reserve_operation(intent)
        normalized_receipt = {key: value for key, value in receipt.items() if key != "raw_capture_v1"}
        board.read_task = lambda task_id: BoardSnapshot.from_dict(normalized_receipt) if task_id == "old-piece" else BoardSnapshot(
            native_task={"id": "anchor-A", "status": "blocked"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="active-digest")
        def verify(action):
            assert "plan_id" not in action.target
            return ActionResult(action.key, "no-op", "v2 unknown receipt reconciled", receipt)
        board.verify_effect = verify
        result = ctl.prepare_active_piece("plan-1", target0.ticket_id)
        assert result["outcome"] == "held" and result["actions_attempted"] == 0, result
    finally:
        store.close()
