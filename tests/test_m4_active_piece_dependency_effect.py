from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from tests.test_m4_active_piece_preparation import native_fixture
from tests.test_m4_active_piece_dependency_authority import (
    SCOPE, _batch_fixture, _database_rows, _seed_two,
)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def _snapshot_for_raw(raw):
    from local_first_orchestrator.hermes_board import HermesBoardAdapter
    data = {"native_task": raw["task"], "parents": [{"id": task_id} for task_id in raw.get("parents", [])],
            "runs": [], "comments": raw.get("comments", []), "events": raw.get("events", []),
            "attachments": raw.get("attachments", [])}
    return {**data, "observed_at": "unit", "digest": HermesBoardAdapter._digest(data)}


def _link_readback(before_source, before_child, after_source, after_child, started=2, ended=3):
    from local_first_orchestrator.hermes_board import validate_accepted_first_link_transition
    transition = validate_accepted_first_link_transition(
        before_source, before_child, after_source, after_child,
        source_id=before_source["task"]["id"], child_id=before_child["task"]["id"],
        command_started_seconds=started, command_ended_seconds=ended)
    return {"kind": "accepted_active_tranche_native_link_readback_v1",
            "source": after_source, "child": after_child,
            "source_snapshot": _snapshot_for_raw(after_source),
            "child_snapshot": _snapshot_for_raw(after_child),
            "time_window": {"started": started, "ended": ended},
            "transition": {"source_id": transition.source_id, "child_id": transition.child_id,
                           "event": _plain(transition.event)}}


def test_locked_helper_matches_public_proof_and_does_not_reacquire_lock(tmp_path, monkeypatch):
    coordinator, store, _board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        acquire_calls = []
        original_acquire = coordinator.lock.acquire
        coordinator.lock.acquire = lambda: (acquire_calls.append("acquire"), original_acquire())[1]
        with coordinator.lock:
            locked = coordinator._active_piece_dependency_authority_locked(
                "plan-1", "TK-B", "TK-A")
            assert coordinator.lock.assert_held() is None
        public = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert dict(locked) == dict(public)
        assert acquire_calls == ["acquire", "acquire"]
        assert locked["frozen_create_receipts"] == public["frozen_create_receipts"]
    finally:
        store.close()


def test_locked_helper_rejects_unlocked_before_board_or_store_access(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        reads = []
        writes = []
        original_read, original_write = board.read_task, store.read_scope
        board.read_task = lambda task_id: (reads.append(task_id), original_read(task_id))[1]
        store.read_scope = lambda *args, **kwargs: (writes.append("read"), original_write(*args, **kwargs))[1]
        with pytest.raises(Exception):
            coordinator._active_piece_dependency_authority_locked("plan-1", "TK-B", "TK-A")
        assert reads == []
        assert writes == []
    finally:
        store.close()


def test_locked_helper_preserves_full_barrier_and_invalid_edge_read_only(tmp_path, monkeypatch):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        before = _database_rows(store)
        reads = []
        original = board.read_task
        board.read_task = lambda task_id: (reads.append(task_id), original(task_id))[1]
        with coordinator.lock:
            with pytest.raises(ValueError):
                coordinator._active_piece_dependency_authority_locked("plan-1", "TK-A", "TK-B")
        assert reads == []
        assert _database_rows(store) == before

        # A third accepted tranche card is independently required by the barrier.
        from tests.test_m4_active_piece_dependency_authority import _three_card_fixture
        third_path = tmp_path / "third"
        third_path.mkdir(mode=0o700)
        third_path.chmod(0o700)
        coordinator2, store2, board2, cards2 = _three_card_fixture(third_path, monkeypatch)
        try:
            for ticket in ("TK-A", "TK-B", "TK-C"):
                assert coordinator2.prepare_active_piece("plan-1", ticket)["outcome"] == "held"
            original_c = cards2["native-TK-C"]
            cards2["native-TK-C"] = type(original_c)(native_task=original_c.native_task,
                parents=original_c.parents, runs=original_c.runs,
                comments=({"body": "drift"},), events=original_c.events,
                attachments=original_c.attachments, observed_at=original_c.observed_at,
                digest=original_c.digest)
            before_c = _database_rows(store2)
            with coordinator2.lock:
                with pytest.raises(ValueError):
                    coordinator2._active_piece_dependency_authority_locked("plan-1", "TK-B", "TK-A")
            assert _database_rows(store2) == before_c
        finally:
            store2.close()
    finally:
        store.close()


def test_persisted_context_is_non_authorizing_frozen_original_identity_without_native_calls(
    tmp_path, monkeypatch,
):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        before = _database_rows(store)
        calls = []
        original_read, original_verify = board.read_task, board.verify_effect
        board.read_task = lambda task_id: (calls.append(("read", task_id)), original_read(task_id))[1]
        board.verify_effect = lambda action: (calls.append(("verify", action.key)), original_verify(action))[1]
        with coordinator.lock:
            context = coordinator._active_piece_dependency_persisted_context_locked(
                "plan-1", "TK-B", "TK-A")
        assert context["kind"] == "accepted_first_link_persisted_context_v1"
        assert context["requires_native_receipt_validation"] is True
        assert calls == []
        assert _database_rows(store) == before

        authority = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert dict(context["authority_identity"]) == {
            key: authority[key] for key in context["authority_identity"]
        }
        assert context["operation_key"] == authority["operation_key"]
        assert context["frozen_create_receipts"] == authority["frozen_create_receipts"]
        with pytest.raises(TypeError):
            context["authority_identity"]["plan_id"] = "changed"

        original = cards["native-TK-A"]
        cards["native-TK-A"] = type(original)(native_task=original.native_task,
            parents=original.parents, runs=original.runs, comments=({"body": "drift"},),
            events=original.events, attachments=original.attachments,
            observed_at=original.observed_at, digest=original.digest)
        calls.clear()
        with coordinator.lock:
            drift_context = coordinator._active_piece_dependency_persisted_context_locked(
                "plan-1", "TK-B", "TK-A")
        assert drift_context["frozen_create_receipts"] == context["frozen_create_receipts"]
        assert calls == []
        with pytest.raises(ValueError, match="fresh native card differs"):
            coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()


def test_native_creation_verifier_rejection_retains_bounded_adapter_diagnostic(tmp_path, monkeypatch):
    """A strict create-receipt barrier exposes its rejected verifier result, not raw card data."""
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        original = board.verify_effect

        def rejected(action):
            result = original(action)
            return ActionResult(action.key, "conflict", "fixture exact verifier rejection", result.readback)

        board.verify_effect = rejected
        with pytest.raises(ValueError, match=(
            "read-only verifier does not prove the immutable create receipt"
            ".*outcome=conflict.*fixture exact verifier rejection"
        )):
            coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
    finally:
        store.close()


def test_native_creation_verifier_content_drift_retains_bounded_digest_diagnostic(tmp_path, monkeypatch):
    """A success-shaped verifier receipt with content drift remains a hard rejection."""
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        original = board.verify_effect

        def drifted(action):
            result = original(action)
            readback = result.readback.copy()
            readback["native_task"] = {**readback["native_task"], "title": "drifted title"}
            return ActionResult(action.key, "verified", "fixture content drift", readback)

        board.verify_effect = drifted
        with pytest.raises(ValueError, match=(
            "outcome=verified.*details=fixture content drift.*readback=drift"
            ".*expected_sha256=[0-9a-f]{64}.*actual_sha256=[0-9a-f]{64}"
        )):
            coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
    finally:
        store.close()


@pytest.mark.parametrize("fault", ("edge", "request", "member", "pause"))
def test_persisted_context_rejects_invalid_edge_request_member_and_pause_before_native_calls(
    tmp_path, monkeypatch, fault,
):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []
        original_read, original_verify = board.read_task, board.verify_effect
        board.read_task = lambda task_id: (calls.append(("read", task_id)), original_read(task_id))[1]
        board.verify_effect = lambda action: (calls.append(("verify", action.key)), original_verify(action))[1]
        target, dependency = "TK-B", "TK-A"
        if fault == "edge":
            target, dependency = "TK-A", "TK-B"
        elif fault == "request":
            coordinator.planning_observer = lambda scope: {"request": {}}
        else:
            original_scope = store.read_scope

            def changed_scope(scope):
                state = original_scope(scope)
                if fault == "member":
                    assert any(member.task_id == "native-TK-B" for member in state["members"])
                    state["members"] = tuple(
                        member for member in state["members"] if member.task_id != "native-TK-B")
                else:
                    from local_first_orchestrator.contracts import PauseIntent
                    state["operator_intent"] = PauseIntent(SCOPE, "operator", 1, True, False)
                return state

            store.read_scope = changed_scope
        with coordinator.lock:
            with pytest.raises((ValueError, KeyError)):
                coordinator._active_piece_dependency_persisted_context_locked(
                    "plan-1", target, dependency)
        assert calls == []
    finally:
        store.close()


def test_production_raw_prebarrier_is_read_only_and_rejects_nonfirst_edge(tmp_path):
    """The adapter seam captures only the exact empty first-edge barrier."""
    from tests.test_m4_accepted_link_adapter import _RawCli, _adapter

    cli = _RawCli()
    board, action = _adapter(tmp_path, cli)
    captured = board.read_accepted_first_link_prebarrier(action, "native-A", "native-B")
    assert captured == {"source_raw": cli.cards["native-A"], "child_raw": cli.cards["native-B"]}
    assert not any(call[0] == "link" for call in cli.calls)

    cli.cards["native-A"]["children"] = ["native-B"]
    with pytest.raises(ValueError, match="first edge"):
        board.read_accepted_first_link_prebarrier(action, "native-A", "native-B")


def test_native_first_link_operation_preparation(tmp_path, native_fixture, monkeypatch):
    """A production adapter reserves one immutable pending proof and sends nothing."""
    from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal

    (_board, _anchor, _workspace, adapter, _membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch,
                                             proposal_factory=_batch_proposal)
    try:
        assert controller.prepare_active_tranche(accepted["plan_id"], request_id=request_id)["outcome"] == "held"
        members_before = tuple(store.read_scope(scope)["members"])
        budgets_before = tuple(store.read_scope(scope)["budget_events"])
        invokes = []
        original = adapter._invoke
        monkeypatch.setattr(adapter, "_invoke", lambda *args, **kwargs: (
            invokes.append(args), original(*args, **kwargs))[1])
        with controller.lock:
            first = controller._prepare_accepted_first_link_operation_locked(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
            second = controller._prepare_accepted_first_link_operation_locked(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
        assert first == second and first.phase == "pending"
        assert first.effect == "link" and first.retry == {"reconcile_only": True}
        proof = first.to_dict()["before_evidence"]
        assert proof["kind"] == "accepted_active_tranche_native_link_prebarrier_v2"
        assert proof["request_id"] == request_id
        assert proof["source_raw"]["children"] == [] and proof["child_raw"]["parents"] == []
        assert proof["expected_source_raw"]["children"] == [first.target["child_task_id"]]
        assert not any(args and args[0] == "link" for args in invokes)
        state = store.read_scope(scope)
        assert tuple(state["members"]) == members_before
        assert tuple(state["budget_events"]) == budgets_before
        assert len([op for op in state["operations"] if op.key == first.key]) == 1
    finally:
        store.close()


def test_first_link_pending_raw_replay_rejects_stored_and_fresh_topology_forgery_without_effects(
    tmp_path, native_fixture, monkeypatch,
):
    """Pending replay rereads exact raw cards; synthetic store data is never authority."""
    from local_first_orchestrator.contracts import PauseIntent
    from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal

    (_board, _anchor, _workspace, adapter, _membership, _cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch,
                                             proposal_factory=_batch_proposal)
    try:
        assert controller.prepare_active_tranche(accepted["plan_id"], request_id=request_id)["outcome"] == "held"
        with controller.lock:
            original = controller._prepare_accepted_first_link_operation_locked(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
        stored_proof = original.to_dict()["before_evidence"]
        state_before = store.read_scope(scope)
        members_before = tuple(state_before["members"])
        budgets_before = tuple(state_before["budget_events"])
        reserve_calls, invoke_calls, reader_calls = [], [], []
        original_scope, original_reserve = store.read_scope, store.reserve_operation
        original_invoke = adapter._invoke
        original_reader = adapter.read_accepted_first_link_prebarrier
        mode = {"fault": None, "reader_finished": False}

        def replay_scope(*args, **kwargs):
            state = original_scope(*args, **kwargs)
            if mode["fault"] in {"stored-children", "stored-extra"}:
                forged = copy.deepcopy(stored_proof)
                if mode["fault"] == "stored-children":
                    forged["source_raw"]["children"] = ["foreign-task"]
                    forged["expected_source_raw"]["children"] = [original.target["child_task_id"]]
                else:
                    forged["source_raw"]["unknown_raw_field"] = {"preserve": "this"}
                    forged["expected_source_raw"]["unknown_raw_field"] = {"preserve": "this"}
                state["operations"] = tuple(
                    dataclasses.replace(operation, before_evidence=forged)
                    if operation.key == original.key else operation
                    for operation in state["operations"]
                )
            elif mode["fault"] == "post-reader-pause" and mode["reader_finished"]:
                state["operator_intent"] = PauseIntent(scope, "operator", 1, True, False)
            return state

        def fresh_reader(*args, **kwargs):
            reader_calls.append(args)
            result = original_reader(*args, **kwargs)
            mode["reader_finished"] = True
            return result

        def invoke(*args, **kwargs):
            invoke_calls.append(args)
            result = original_invoke(*args, **kwargs)
            if (mode["fault"] in {"fresh-children", "fresh-extra"}
                    and args[:2] == ("show", original.target["source_task_id"]) and result is not None):
                detached = copy.deepcopy(result)
                if mode["fault"] == "fresh-children":
                    detached["children"] = ["foreign-task"]
                else:
                    detached["unknown_raw_field"] = {"preserve": "this"}
                return detached
            return result

        monkeypatch.setattr(store, "read_scope", replay_scope)
        monkeypatch.setattr(store, "reserve_operation", lambda intent: (
            reserve_calls.append(intent), original_reserve(intent))[1])
        monkeypatch.setattr(adapter, "read_accepted_first_link_prebarrier", fresh_reader)
        monkeypatch.setattr(adapter, "_invoke", invoke)

        # A valid replay must freshly invoke the native raw reader and reserve nothing.
        with controller.lock:
            assert controller._prepare_accepted_first_link_operation_locked(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id) == original
        assert reader_calls and reserve_calls == []

        for fault in ("stored-children", "stored-extra", "fresh-children", "fresh-extra", "post-reader-pause"):
            mode.update(fault=fault, reader_finished=False)
            readers_before = len(reader_calls)
            with controller.lock:
                with pytest.raises(ValueError):
                    controller._prepare_accepted_first_link_operation_locked(
                        accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
            assert len(reader_calls) == readers_before + 1
            assert reserve_calls == []
            assert original.to_dict()["before_evidence"] == stored_proof
            state = original_scope(scope)
            assert tuple(state["members"]) == members_before
            assert tuple(state["budget_events"]) == budgets_before
            assert next(operation for operation in state["operations"] if operation.key == original.key) == original

        assert not any(args and args[0] == "link" for args in invoke_calls)
    finally:
        store.close()


@pytest.mark.parametrize("fault", ("lower-one", "lower-future", "lower-bool", "observed-malformed"))
def test_first_link_pending_forged_timestamp_evidence_rejects_without_another_reservation_or_link(
    tmp_path, native_fixture, monkeypatch, fault,
):
    """A replay validates the journal's original clock evidence, never rebuilding it."""
    from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal

    (_board, _anchor, _workspace, adapter, _membership, _cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch,
                                             proposal_factory=_batch_proposal)
    try:
        assert controller.prepare_active_tranche(accepted["plan_id"], request_id=request_id)["outcome"] == "held"
        with controller.lock:
            original = controller._prepare_accepted_first_link_operation_locked(
                accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
        reservation_calls, native_calls = [], []
        original_reserve, original_invoke = store.reserve_operation, adapter._invoke
        monkeypatch.setattr(store, "reserve_operation", lambda intent: (
            reservation_calls.append(intent), original_reserve(intent))[1])
        monkeypatch.setattr(adapter, "_invoke", lambda *args, **kwargs: (
            native_calls.append(args), original_invoke(*args, **kwargs))[1])
        original_scope = store.read_scope

        def forged_scope(*args, **kwargs):
            state = original_scope(*args, **kwargs)
            forged = dict(original.to_dict()["before_evidence"])
            if fault == "lower-one":
                forged["event_lower_bound_seconds"] = 1
            elif fault == "lower-future":
                forged["event_lower_bound_seconds"] = int(datetime.now(timezone.utc).timestamp()) + 100_000
            elif fault == "lower-bool":
                forged["event_lower_bound_seconds"] = True
            else:
                forged["source_snapshot"]["observed_at"] = "not-a-native-utc-time"
            state["operations"] = tuple(
                dataclasses.replace(operation, before_evidence=forged)
                if operation.key == original.key else operation
                for operation in state["operations"]
            )
            return state

        monkeypatch.setattr(store, "read_scope", forged_scope)
        with controller.lock:
            with pytest.raises(ValueError):
                controller._prepare_accepted_first_link_operation_locked(
                    accepted["plan_id"], "TK-B", "TK-A", request_id=request_id)
        assert reservation_calls == []
        assert not any(args and args[0] == "link" for args in native_calls)
        assert len([operation for operation in original_scope(scope)["operations"]
                    if operation.key == original.key]) == 1
    finally:
        store.close()


@pytest.mark.parametrize("lower_bound", (1, 1_000_100, True))
def test_first_link_lower_bound_validator_rejects_out_of_range_and_non_plain_integer(lower_bound):
    from local_first_orchestrator.coordinator import Coordinator

    observed_at = datetime.fromtimestamp(1_000_000, timezone.utc).isoformat()
    before = {
        "event_lower_bound_seconds": lower_bound,
        "captured_clock_seconds": 1_000_000,
        "source_snapshot": {"observed_at": observed_at},
        "child_snapshot": {"observed_at": observed_at},
        "source_raw": {"task": {"created_at": 999_999}, "events": [{"created_at": 1_000_000}]},
        "child_raw": {"task": {"created_at": 999_999}, "events": []},
    }
    with pytest.raises(ValueError):
        Coordinator._validate_first_link_event_lower_bound(before, current_trusted_clock_seconds=1_000_000)


def test_first_link_lower_bound_validator_rejects_malformed_original_observation_time():
    from local_first_orchestrator.coordinator import Coordinator

    before = {
        "event_lower_bound_seconds": 1_000_000,
        "captured_clock_seconds": 1_000_000,
        "source_snapshot": {"observed_at": "not-a-native-utc-time"},
        "child_snapshot": {"observed_at": datetime.fromtimestamp(1_000_000, timezone.utc).isoformat()},
        "source_raw": {"task": {}, "events": []},
        "child_raw": {"task": {}, "events": []},
    }
    with pytest.raises(ValueError):
        Coordinator._validate_first_link_event_lower_bound(before, current_trusted_clock_seconds=1_000_000)


def test_execute_first_link_returns_adapter_conflict_without_success_readback_validation(
    tmp_path, monkeypatch,
):
    """A rejected native result is diagnostic evidence, never a malformed success receipt."""
    from local_first_orchestrator.contracts import ActionResult, OperationIntent

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []

        def prepare(plan_id, ticket_id, dependency_id, *, request_id=None):
            authority = coordinator._active_piece_dependency_authority_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)
            target = {"kind": "accepted_active_tranche_native_link_v1",
                      "source_task_id": authority["source_task_id"],
                      "child_task_id": authority["target_task_id"],
                      "operation_key": authority["operation_key"]}
            child = next(item for item in authority["frozen_create_receipts"]
                         if item["task_id"] == target["child_task_id"])
            return store.reserve_operation(OperationIntent(
                authority["operation_key"], SCOPE, target, "link", child["digest"],
                {"synthetic": "adapter-diagnostic-seam"}, None, None,
                {"reconcile_only": True}, "pending",
            ))

        def rejected_link(action, parent_task_id, child_task_id):
            calls.append((action, parent_task_id, child_task_id))
            store.begin_effect_attempt(SCOPE, action.key)
            return ActionResult(action.key, "conflict", "native adapter rejected exact frozen edge", None)

        monkeypatch.setattr(coordinator, "_prepare_accepted_first_link_operation_locked", prepare)
        board.link = rejected_link
        result = coordinator.execute_accepted_first_link("plan-1", "TK-B", "TK-A")

        assert result["outcome"] == "partial"
        assert result["reason"] == "accepted-link adapter conflict: native adapter rejected exact frozen edge"
        assert "readback" not in result["reason"]
        assert len(calls) == 1
        operation = next(item for item in store.read_scope(SCOPE)["operations"]
                         if item.key == result["operation_key"])
        assert operation.phase == "unknown" and operation.readback is None
    finally:
        store.close()


def test_execute_first_link_reserves_attempts_once_and_reconciles_applied_receipt_without_release(
    tmp_path, monkeypatch,
):
    """The first executable coordinator boundary changes only its link intent."""
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    calls = []
    try:
        _seed_two(coordinator)
        members_before = tuple(store.read_scope(SCOPE)["members"])
        budgets_before = tuple(store.read_scope(SCOPE)["budget_events"])
        from tests.test_m4_coordinator_active_tranche_inspection import _raw
        post_raw = {}

        def link(action, parent_task_id, child_task_id):
            calls.append(("link", action, parent_task_id, child_task_id))
            store.begin_effect_attempt(SCOPE, action.key)
            operation = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == action.key)
            assert operation.phase == "unknown"
            before_source = copy.deepcopy(_raw(cards[parent_task_id]))
            before_child = copy.deepcopy(_raw(cards[child_task_id]))
            source, child = copy.deepcopy(before_source), copy.deepcopy(before_child)
            source["children"] = [child_task_id]
            child["parents"] = [parent_task_id]
            child["events"].append({"created_at": 2, "kind": "linked",
                                    "payload": {"parent": parent_task_id, "child": child_task_id},
                                    "run_id": None})
            receipt = _link_readback(before_source, before_child, source, child)
            post_raw.update(before_source=before_source, before_child=before_child,
                            source=source, child=child, readback=receipt)
            return ActionResult(action.key, "verified", "exact link readback", receipt)

        original_verify = board.verify_effect
        observations, acknowledgements = [], []
        original_observe, original_ack = store.record_effect_observation, store.ack_effect

        def observe(*args, **kwargs):
            observations.append((args, kwargs))
            return original_observe(*args, **kwargs)

        def acknowledge(*args, **kwargs):
            acknowledgements.append((args, kwargs))
            return original_ack(*args, **kwargs)

        def verify(action):
            if action.effect != "link":
                return original_verify(action)
            calls.append(("verify", action))
            # Production readbacks legitimately refresh observation/time fields.
            # Applied reconciliation must validate this independently and must not
            # try to replace the immutable stored acknowledgement with it.
            return ActionResult(action.key, "verified", "fresh exact link readback",
                                copy.deepcopy(post_raw["readback"]))

        board.link, board.verify_effect = link, verify
        monkeypatch.setattr(store, "record_effect_observation", observe)
        monkeypatch.setattr(store, "ack_effect", acknowledge)

        def prepare(plan_id, ticket_id, dependency_id, *, request_id=None):
            from local_first_orchestrator.contracts import OperationIntent
            authority = coordinator._active_piece_dependency_authority_locked(
                plan_id, ticket_id, dependency_id, request_id=request_id)
            target = {"kind": "accepted_active_tranche_native_link_v1",
                      "source_task_id": authority["source_task_id"],
                      "child_task_id": authority["target_task_id"],
                      "operation_key": authority["operation_key"]}
            child = next(item for item in authority["frozen_create_receipts"]
                         if item["task_id"] == target["child_task_id"])
            source_raw = _raw(cards[target["source_task_id"]])
            child_raw = _raw(cards[target["child_task_id"]])
            expected_source = copy.deepcopy(source_raw)
            expected_source["children"] = [target["child_task_id"]]
            before = {"kind": "accepted_active_tranche_native_link_prebarrier_v2",
                      "authority": dict(authority), "source_raw": source_raw,
                      "child_raw": child_raw, "expected_source_raw": expected_source,
                      "source_snapshot": _snapshot_for_raw(source_raw),
                      "child_snapshot": _snapshot_for_raw(child_raw),
                      "request_id": None, "captured_clock_seconds": 1,
                      "event_lower_bound_seconds": 1}
            return store.reserve_operation(OperationIntent(
                authority["operation_key"], SCOPE, target, "link", child["digest"],
                before, None, None, {"reconcile_only": True}, "pending",
            ))

        monkeypatch.setattr(coordinator, "_prepare_accepted_first_link_operation_locked", prepare)
        first = coordinator.execute_accepted_first_link("plan-1", "TK-B", "TK-A")
        assert first["outcome"] == "linked" and first["actions_attempted"] == 1, first
        operation = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == first["operation_key"])
        assert operation.phase == "applied" and operation.effect == "link"
        assert tuple(store.read_scope(SCOPE)["members"]) == members_before
        assert tuple(store.read_scope(SCOPE)["budget_events"]) == budgets_before
        stored_readback = operation.readback
        assert len(observations) == len(acknowledgements) == 1

        replay = coordinator.execute_accepted_first_link("plan-1", "TK-B", "TK-A")
        assert replay["outcome"] == "linked" and replay["actions_attempted"] == 0, replay
        assert [entry[0] for entry in calls] == ["link", "verify"]
        replayed = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == first["operation_key"])
        assert replayed.readback == stored_readback
        assert len(observations) == len(acknowledgements) == 1
    finally:
        store.close()
