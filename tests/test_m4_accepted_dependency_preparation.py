from __future__ import annotations

import json
import time
from collections.abc import Mapping

import pytest

from local_first_orchestrator.contracts import PauseIntent
from tests.test_m4_active_piece_dependency_authority import _batch_fixture, _database_rows, _seed_two


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def _frozen_raw_cards(store, scope):
    """Rebuild the production raw-reader transport from immutable create captures."""
    cards = {}
    for operation in store.read_scope(scope)["operations"]:
        if operation.effect != "create_held":
            continue
        capture = _plain(operation.readback["raw_capture_v1"])
        show = capture["show"]
        task_id = show["task"]["id"]
        cards[task_id] = {**show, "runs": capture["runs"],
                          "show_has_runs": "runs" in show,
                          "show_runs": show.get("runs") if "runs" in show else None}
    return cards


def _install_raw_reader(board, store, coordinator, *, mutate=None):
    calls = []

    def reader(_scope, task_ids):
        calls.append(tuple(task_ids))
        result = _frozen_raw_cards(store, coordinator.scope)
        if mutate is not None:
            mutate(result)
        return {task_id: result[task_id] for task_id in task_ids}

    board.read_accepted_active_tranche_raw_cards = reader
    return calls


def _assert_only_one_operation_row_added(before, after):
    before_tables, after_tables = dict(before), dict(after)
    assert set(before_tables) == set(after_tables)
    changed = {name: (before_tables[name], after_tables[name]) for name in before_tables
               if before_tables[name] != after_tables[name]}
    assert set(changed) == {"operation_intents"}
    old, new = changed["operation_intents"]
    assert len(new) == len(old) + 1
    assert new[:-1] == old


def _inject_frozen_opaque_fields(store, coordinator):
    """Model a valid historic raw capture with fields the typed receipt omits."""
    board_id, anchor = coordinator.scope["board_id"], coordinator.scope["anchor_task_id"]
    rows = tuple(store.connection.execute(
        "SELECT operation_key, intent_json FROM operation_intents "
        "WHERE board_id = ? AND anchor_task_id = ?", (board_id, anchor)))
    for key, encoded in rows:
        body = json.loads(encoded)
        if body["effect"] != "create_held":
            continue
        show = body["readback"]["raw_capture_v1"]["show"]
        show["opaque_top_level"] = {"stable": ["x"]}
        show["attachments"] = [{"id": "attachment-stable"}]
        show["task"]["opaque_task_field"] = {"stable": True}
        store.connection.execute("UPDATE operation_intents SET intent_json = ? "
                                 "WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
                                 (json.dumps(body, sort_keys=True, separators=(",", ":")),
                                  board_id, anchor, key))
    store.connection.commit()


def test_prepare_first_declared_edge_reserves_exact_pending_intent_without_native_effect(
    tmp_path, monkeypatch,
):
    """Tracer bullet: a declared held edge becomes one durable v2 pending intent."""
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = _install_raw_reader(board, store, coordinator)
        before = _database_rows(store)
        members_before = tuple(store.read_scope(coordinator.scope)["members"])
        budgets_before = tuple(store.read_scope(coordinator.scope)["budget_events"])
        prepared = coordinator.prepare_accepted_dependency_link(
            "plan-1", "TK-B", "TK-A",
        )
        assert prepared["outcome"] == "pending", prepared
        assert prepared["actions_attempted"] == 0
        assert prepared["effect"] == "link"
        assert prepared["target"]["kind"] == "accepted_active_tranche_native_link_v2"
        assert calls == [("native-TK-A", "native-TK-B")]
        state = store.read_scope(coordinator.scope)
        operation = next(op for op in state["operations"] if op.key == prepared["operation_key"])
        assert operation.phase == "pending" and operation.outcome is None and operation.readback is None
        assert operation.retry == {"reconcile_only": True}
        assert operation.before_evidence["kind"] == "accepted_active_tranche_native_link_preparation_v2"
        assert tuple(state["members"]) == members_before
        assert tuple(state["budget_events"]) == budgets_before
        _assert_only_one_operation_row_added(before, _database_rows(store))
    finally:
        store.close()


def test_prepare_preserves_stable_opaque_raw_fields(tmp_path, monkeypatch):
    """Opaque raw wire fields are accepted only when their exact values survive."""
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        _inject_frozen_opaque_fields(store, coordinator)
        _install_raw_reader(board, store, coordinator)
        prepared = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert prepared["outcome"] == "pending"
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["comments", "attachments", "opaque", "task_opaque", "field_absence"])
def test_prepare_rejects_any_raw_capture_value_or_presence_drift_without_reservation(
    tmp_path, monkeypatch, mutation,
):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        _inject_frozen_opaque_fields(store, coordinator)

        def drift(cards):
            card = cards["native-TK-A"]
            if mutation == "comments":
                card["comments"] = [{"body": "changed"}]
            elif mutation == "attachments":
                card["attachments"] = [{"id": "attachment-changed"}]
            elif mutation == "opaque":
                card["opaque_top_level"] = {"stable": ["changed"]}
            elif mutation == "task_opaque":
                card["task"]["opaque_task_field"] = {"stable": False}
            else:
                del card["latest_summary"]

        calls = _install_raw_reader(board, store, coordinator, mutate=drift)
        before = _database_rows(store)
        with pytest.raises(ValueError, match="current raw card"):
            coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert calls == [("native-TK-A", "native-TK-B")]
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("target, dependency", [
    ("TK-A", "TK-B"), ("TK-A", "TK-A"), ("TK-B", "TK-FUTURE"), (None, "TK-A"),
])
def test_prepare_rejects_invalid_edge_before_raw_reader_or_reservation(
    tmp_path, monkeypatch, target, dependency,
):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []
        board.read_accepted_active_tranche_raw_cards = lambda *_: calls.append(True)
        before = _database_rows(store)
        with pytest.raises(ValueError):
            coordinator.prepare_accepted_dependency_link("plan-1", target, dependency)
        assert calls == []
        assert _database_rows(store) == before
    finally:
        store.close()


def test_prepare_unknown_operation_refuses_before_raw_reader_or_reservation(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        _install_raw_reader(board, store, coordinator)
        prepared = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        row = store.connection.execute("SELECT intent_json FROM operation_intents WHERE operation_key = ?",
                                       (prepared["operation_key"],)).fetchone()
        body = json.loads(row[0]); body["phase"] = "unknown"
        store.connection.execute("UPDATE operation_intents SET intent_json = ? WHERE operation_key = ?",
                                 (json.dumps(body, sort_keys=True, separators=(",", ":")), prepared["operation_key"]))
        store.connection.commit()
        calls = []
        board.read_accepted_active_tranche_raw_cards = lambda *_: calls.append(True)
        before = _database_rows(store)
        with pytest.raises(ValueError, match="reconciliation"):
            coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert calls == []
        assert _database_rows(store) == before
    finally:
        store.close()


def test_execute_v2_binds_real_link_event_to_pre_send_through_postread_window(tmp_path, monkeypatch):
    """A native integer-second event may precede the coordinator's final read clock."""
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        current = _frozen_raw_cards(store, coordinator.scope)
        link_calls = []

        def reader(_scope, task_ids):
            return {task_id: _plain(current[task_id]) for task_id in task_ids}

        def link(action, source_task_id, target_task_id):
            link_calls.append((source_task_id, target_task_id))
            current[source_task_id]["children"].append(target_task_id)
            current[target_task_id]["parents"].append(source_task_id)
            # Exact native shape observed in the preserved parent fixture.
            current[target_task_id]["events"].append({
                "created_at": int(time.time()), "kind": "linked",
                "payload": {"parent": source_task_id, "child": target_task_id}, "run_id": None,
            })
            return ActionResult(action.key, "verified", "fixture linked", None)

        # The first native-shaped event is captured in its original [100, 101]
        # command window.  A later applied replay reads at a far-advanced clock:
        # that fresh observation may establish present plausibility, but may not
        # replace either persisted command boundary.
        clocks = iter((100.0, 101.0, 101.0, 100_000.0))
        clock_calls = []

        def clock():
            value = next(clocks)
            clock_calls.append(value)
            return value

        board.read_accepted_active_tranche_raw_cards = reader
        board.link = link
        monkeypatch.setattr("local_first_orchestrator.coordinator.time.time", clock)
        result = coordinator.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert result["outcome"] == "linked" and result["actions_attempted"] == 1
        operation = next(item for item in store.read_scope(coordinator.scope)["operations"]
                         if item.key == result["operation_key"])
        assert operation.readback["time_window"] == {"started": 100, "ended": 101}
        event = operation.readback["observed_cards"]["native-TK-B"]["events"][-1]
        assert event == {"created_at": 101, "kind": "linked",
                         "payload": {"parent": "native-TK-A", "child": "native-TK-B"}, "run_id": None}
        before_replay = _database_rows(store)
        stored_receipt = _plain(operation.readback)
        replay = coordinator.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert replay == {"outcome": "linked", "operation_key": result["operation_key"], "actions_attempted": 0}
        assert link_calls == [("native-TK-A", "native-TK-B")]
        assert clock_calls == [100.0, 101.0, 101.0, 100_000.0]
        replayed = next(item for item in store.read_scope(coordinator.scope)["operations"]
                        if item.key == result["operation_key"])
        assert _plain(replayed.readback) == stored_receipt
        assert _database_rows(store) == before_replay
    finally:
        store.close()



def test_prepare_second_serial_edge_uses_validated_first_link_receipt_as_raw_baseline(tmp_path, monkeypatch):
    """A first native-shaped link authorizes the next edge without relaxing raw drift checks."""
    from local_first_orchestrator.contracts import ActionResult
    from tests.test_m4_active_piece_dependency_authority import (
        _serial_three_card_proposal, _three_card_fixture,
    )

    coordinator, store, board, _cards = _three_card_fixture(
        tmp_path, monkeypatch, proposal_factory=_serial_three_card_proposal,
    )
    try:
        for ticket_id in ("TK-A", "TK-B", "TK-C"):
            assert coordinator.prepare_active_piece("plan-1", ticket_id)["outcome"] == "held"
        current = _frozen_raw_cards(store, coordinator.scope)
        links = []

        def reader(_scope, task_ids):
            return {task_id: _plain(current[task_id]) for task_id in task_ids}

        def link(action, source_task_id, target_task_id):
            links.append((source_task_id, target_task_id))
            current[source_task_id]["children"].append(target_task_id)
            current[target_task_id]["parents"].append(source_task_id)
            current[target_task_id]["events"].append({
                "created_at": 101, "kind": "linked",
                "payload": {"parent": source_task_id, "child": target_task_id}, "run_id": None,
            })
            return ActionResult(action.key, "verified", "fixture linked", None)

        monkeypatch.setattr(board, "read_accepted_active_tranche_raw_cards", reader, raising=False)
        monkeypatch.setattr(board, "link", link, raising=False)
        clocks = iter((100.0, 101.0))
        monkeypatch.setattr("local_first_orchestrator.coordinator.time.time", lambda: next(clocks))
        first = coordinator.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert first["outcome"] == "linked" and first["actions_attempted"] == 1
        first_operation = next(op for op in store.read_scope(coordinator.scope)["operations"]
                               if op.key == first["operation_key"])
        stored_first_receipt = _plain(first_operation.readback)

        second = coordinator.prepare_accepted_dependency_link("plan-1", "TK-C", "TK-B")
        assert second["outcome"] == "pending" and second["actions_attempted"] == 0
        assert links == [("native-TK-A", "native-TK-B")]
        replayed_first = next(op for op in store.read_scope(coordinator.scope)["operations"]
                              if op.key == first["operation_key"])
        assert _plain(replayed_first.readback) == stored_first_receipt
    finally:
        store.close()


def test_prepared_link_refuses_third_member_raw_drift_before_claim_or_send(tmp_path, monkeypatch):
    """A prepared edge still fences every stored source, including an uninvolved third card."""
    from tests.test_m4_active_piece_dependency_authority import _three_card_fixture

    coordinator, store, board, _cards = _three_card_fixture(tmp_path, monkeypatch)
    try:
        for ticket_id in ("TK-A", "TK-B", "TK-C"):
            assert coordinator.prepare_active_piece("plan-1", ticket_id)["outcome"] == "held"
        current = _frozen_raw_cards(store, coordinator.scope)
        board.read_accepted_active_tranche_raw_cards = (
            lambda _scope, task_ids: {task_id: _plain(current[task_id]) for task_id in task_ids}
        )
        prepared = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        before = _database_rows(store)
        link_calls = []

        def reader(_scope, task_ids):
            observed = {task_id: _plain(current[task_id]) for task_id in task_ids}
            observed["native-TK-C"]["task"]["title"] = "human third-member drift"
            return observed

        board.read_accepted_active_tranche_raw_cards = reader
        board.link = lambda *args: link_calls.append(args)
        rejected = coordinator.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")

        assert rejected["outcome"] == "conflict"
        assert rejected["actions_attempted"] == 0
        assert prepared["operation_key"] in rejected["reason"] or "pre-send barrier" in rejected["reason"]
        assert link_calls == []
        assert _database_rows(store) == before
    finally:
        store.close()


def test_prepare_rejects_hostile_reader_container_before_hook_invocation(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)

        class HostileDict(dict):
            def items(self):
                raise AssertionError("untrusted hook invoked")

        board.read_accepted_active_tranche_raw_cards = lambda *_: HostileDict()
        before = _database_rows(store)
        with pytest.raises(ValueError, match="plain JSON"):
            coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()


def test_prepare_pause_rejects_before_raw_reader_or_reservation(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        store.set_operator_intent(PauseIntent(coordinator.scope, "operator", 1, True, False))
        calls = []
        board.read_accepted_active_tranche_raw_cards = lambda *_: calls.append(True)
        before = _database_rows(store)
        with pytest.raises(ValueError, match="pause/cancellation"):
            coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert calls == []
        assert _database_rows(store) == before
    finally:
        store.close()


def test_prepare_exact_pending_repeat_is_idempotent_without_row_change(tmp_path, monkeypatch):
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = _install_raw_reader(board, store, coordinator)
        first = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        before = _database_rows(store)
        second = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert second == first
        assert calls == [("native-TK-A", "native-TK-B"), ("native-TK-A", "native-TK-B")]
        assert _database_rows(store) == before
    finally:
        store.close()


def test_execute_prepared_link_holds_when_pause_arrives_during_executor_raw_read(tmp_path, monkeypatch):
    """The executor's fresh raw barrier fences a pause that follows preparation."""
    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        _install_raw_reader(board, store, coordinator)
        prepared = coordinator.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        before_pause = _database_rows(store)
        budgets_before_pause = tuple(store.read_scope(coordinator.scope)["budget_events"])
        injected_rows = []
        raw_reads = []

        def pause_during_executor_read(scope, task_ids):
            raw_reads.append(tuple(task_ids))
            observed = _frozen_raw_cards(store, coordinator.scope)
            # Preparation already consumed its raw read. This mutation arrives
            # inside execute's fresh raw barrier, before any claim or native send.
            store.set_operator_intent(PauseIntent(scope, "operator", 1, True, False))
            injected_rows[:] = _database_rows(store)
            return {task_id: observed[task_id] for task_id in task_ids}

        board.read_accepted_active_tranche_raw_cards = pause_during_executor_read
        link_calls = []
        board.link = lambda *args: link_calls.append(args)
        result = coordinator.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")

        assert raw_reads == [("native-TK-A", "native-TK-B")]
        assert result == {
            "outcome": "held", "operation_key": prepared["operation_key"],
            "actions_attempted": 0, "reason": "operator pause or cancellation is active",
        }
        assert link_calls == []
        assert _database_rows(store) != before_pause
        assert _database_rows(store) == tuple(injected_rows)
        operation = next(op for op in store.read_scope(coordinator.scope)["operations"]
                         if op.key == prepared["operation_key"])
        assert operation.phase == "pending"
        assert store.read_scope(coordinator.scope)["operator_intent"].active is True
        assert tuple(store.read_scope(coordinator.scope)["budget_events"]) == budgets_before_pause
    finally:
        store.close()
