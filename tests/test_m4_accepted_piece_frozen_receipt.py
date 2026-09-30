"""Pure historical accepted-piece receipt contract; this is not native truth."""
from __future__ import annotations

import copy
from pathlib import Path
from types import MappingProxyType

import pytest

from local_first_orchestrator.contracts import Action, BoardSnapshot
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from tests.test_m4_piece_adapter import SCOPE, adapter, make_action, payload
from tests.test_hermes_board_adapter import FakeKanban
from tests.test_m4_active_piece_preparation import _accepted_plan, native_fixture


def _receipt(board, piece, native_task_id="native-piece-1"):
    marker = board._create_marker(make_action(piece))
    task = {"id": native_task_id, "title": piece.title,
            "body": piece.body + "\n\n" + marker, "assignee": piece.target["assignee"],
            "workspace": piece.target["workspace"], "status": "blocked"}
    data = {"native_task": task, "parents": [], "runs": [], "comments": [], "events": [], "attachments": []}
    snapshot = BoardSnapshot(native_task=task, parents=(), runs=(), comments=(), events=(), attachments=(),
        observed_at="2000-01-01T00:00:00+00:00", digest=board._digest(data))
    return snapshot.to_dict()


def _board(tmp_path, piece):
    fake = FakeKanban(); fake.add("anchor-1")
    board = adapter(tmp_path, fake, lambda *_: piece)
    for name in ("_invoke", "read_task", "verify_effect", "create_held", "_marked_create_matches"):
        setattr(board, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("native access is forbidden")))
    return board


def test_valid_historical_accepted_piece_receipt_is_detached_immutable_and_non_authorizing(tmp_path):
    piece = payload(); board = _board(tmp_path, piece); action = make_action(piece)
    result = board.validate_accepted_piece_frozen_receipt(action, _receipt(board, piece), "native-piece-1")
    assert result["kind"] == "accepted_piece_frozen_receipt_validation_v1"
    assert result["requires_fresh_native_transition_validation"] is True
    assert result["snapshot"].native_task["id"] == "native-piece-1"
    with pytest.raises(TypeError): result["snapshot"].native_task["title"] = "forged"
    with pytest.raises(AttributeError): result["snapshot"].parents.append({"id": "forged"})


@pytest.mark.parametrize("damage", ["body", "title", "workspace", "assignee", "id", "status", "parent", "run", "digest"])
def test_historical_receipt_rejects_each_canonical_identity_or_held_shape_difference(tmp_path, damage):
    piece = payload(); board = _board(tmp_path, piece); raw = _receipt(board, piece)
    if damage == "body": raw["native_task"]["body"] = "forged"
    elif damage == "title": raw["native_task"]["title"] = "forged"
    elif damage == "workspace": raw["native_task"]["workspace"] = "dir:/forged"
    elif damage == "assignee": raw["native_task"]["assignee"] = "forged"
    elif damage == "id": raw["native_task"]["id"] = "forged"
    elif damage == "status": raw["native_task"]["status"] = "ready"
    elif damage == "parent": raw["parents"] = [{"id": "anchor-1"}]
    elif damage == "run": raw["runs"] = [{"id": "run-1"}]
    else: raw["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError): board.validate_accepted_piece_frozen_receipt(make_action(piece), raw, "native-piece-1")


@pytest.mark.parametrize("action_damage", [
    {"title": "forged"}, {"workspace": "dir:/forged"}, {"assignee": "forged"},
    {"ticket_id": "forged"}, {"native_parent": True},
])
def test_historical_receipt_rechecks_closed_action_against_trusted_plan_source(tmp_path, action_damage):
    piece = payload(); board = _board(tmp_path, piece)
    with pytest.raises(ValueError):
        board.validate_accepted_piece_frozen_receipt(make_action(piece, **action_damage), _receipt(board, piece), "native-piece-1")


@pytest.mark.parametrize("field,value", [("key", "forged-key"), ("scope", {"board_id": "other", "anchor_task_id": "anchor-1"})])
def test_historical_receipt_rechecks_action_key_and_scope_against_trusted_plan_source(tmp_path, field, value):
    piece = payload(); board = _board(tmp_path, piece); canonical = make_action(piece)
    action = Action(value if field == "key" else canonical.key,
        value if field == "scope" else canonical.scope, canonical.target, canonical.effect,
        canonical.expected_observed_identity)
    with pytest.raises(ValueError):
        board.validate_accepted_piece_frozen_receipt(action, _receipt(board, piece), "native-piece-1")


def test_historical_receipt_rechecks_resolver_plan_evidence(tmp_path):
    piece = payload(); forged = copy.deepcopy(piece.plan_evidence)
    forged["request"]["request_identity"] = "forged"
    damaged = type("Resolved", (), {"title": piece.title, "body": piece.body, "target": piece.target,
        "idempotency_key": piece.idempotency_key, "plan_evidence": forged})()
    fake = FakeKanban(); fake.add("anchor-1")
    board = adapter(tmp_path, fake, lambda *_: damaged)
    board._invoke = lambda *a, **k: (_ for _ in ()).throw(AssertionError("native access is forbidden"))
    with pytest.raises(ValueError):
        board.validate_accepted_piece_frozen_receipt(make_action(piece), _receipt(board, piece), "native-piece-1")


def test_recomputed_digest_cannot_certify_forged_accepted_description(tmp_path):
    piece = payload(); board = _board(tmp_path, piece); raw = _receipt(board, piece)
    raw["native_task"]["body"] = raw["native_task"]["body"].replace("criterion", "forged-criterion", 1)
    raw["digest"] = board._digest({key: raw[key] for key in ("native_task", "parents", "runs", "comments", "events", "attachments")})
    with pytest.raises(ValueError): board.validate_accepted_piece_frozen_receipt(make_action(piece), raw, "native-piece-1")


@pytest.mark.parametrize("hostile", ["mapping_proxy", "cycle", "oversized"])
def test_historical_receipt_rejects_non_transport_or_unbounded_input_before_hooks_or_allocation(tmp_path, hostile):
    piece = payload(); board = _board(tmp_path, piece); raw = _receipt(board, piece)
    if hostile == "mapping_proxy": raw = MappingProxyType(raw)
    elif hostile == "cycle": raw["comments"].append(raw)
    else: raw["comments"] = [{"body": "x" * 1_000_001}]
    with pytest.raises(ValueError): board.validate_accepted_piece_frozen_receipt(make_action(piece), raw, "native-piece-1")


def test_native_coordinator_created_receipt_validates_without_native_reads(
    tmp_path, native_fixture, monkeypatch,
):
    (_, _, _, board, _, _, coordinator, store, scope, accepted, request_id) = _accepted_plan(
        tmp_path, native_fixture, monkeypatch)
    try:
        prepared = coordinator.prepare_active_piece(
            accepted["plan_id"], "TK-A", request_id=request_id)
        assert prepared["outcome"] == "held", prepared
        intent = next(op for op in store.read_scope(scope)["operations"]
                      if op.key == prepared["operation_key"])
        original = coordinator._action_from_intent(intent)
        target = {key: value for key, value in original.to_dict()["target"].items()
                  if key != "plan_id"}
        action = Action(original.key, original.scope, target, original.effect,
                        original.expected_observed_identity)
        snapshot = coordinator._snapshot_from_readback(intent.readback)
        assert snapshot is not None
        before = store.read_scope(scope)
        def forbidden(*args, **kwargs):
            raise AssertionError("historical validation attempted native access")
        for name in ("_invoke", "read_task", "verify_effect", "create_held", "_marked_create_matches"):
            monkeypatch.setattr(board, name, forbidden)
        with coordinator.lock:
            result = board.validate_accepted_piece_frozen_receipt(
                action, snapshot.to_dict(), prepared["task_id"])
        assert result["requires_fresh_native_transition_validation"] is True
        assert result["snapshot"].to_dict() == snapshot.to_dict()
        assert store.read_scope(scope) == before
    finally:
        store.close()
