from __future__ import annotations

import copy
import json

import pytest

from tests.test_m4_active_piece_dependency_authority import _batch_fixture, _database_rows, _seed_two
from tests.test_m4_active_piece_preparation import native_fixture


def _raw(snapshot):
    task = dict(snapshot.native_task)
    if "workspace_path" not in task and task.get("workspace", "").startswith("dir:"):
        task["workspace_path"] = task["workspace"][4:]
    return {"task": task, "parents": [item["id"] for item in snapshot.parents],
            "children": [], "runs": list(snapshot.runs), "events": list(snapshot.events),
            "comments": list(snapshot.comments), "latest_summary": None,
            **({"attachments": list(snapshot.attachments)} if snapshot.attachments else {})}


@pytest.mark.parametrize("show_runs_present", (False, True))
def test_new_acknowledgement_preserves_show_runs_presence(tmp_path, monkeypatch, show_runs_present):
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held

        def create_with_shape(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            readback = dict(result.readback)
            capture = dict(result.readback["raw_capture_v1"])
            show = dict(capture["show"])
            if show_runs_present:
                show["runs"] = []
            else:
                show.pop("runs", None)
            capture["show"] = show
            readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_with_shape
        result = coordinator.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "held", result
        operation = next(op for op in store.read_scope(coordinator.scope)["operations"]
                         if op.key == result["operation_key"])
        show = operation.readback["raw_capture_v1"]["show"]
        assert ("runs" in show) is show_runs_present
        assert operation.readback["raw_capture_v1"]["runs"] == ()
    finally:
        store.close()


def test_new_acknowledgement_preserves_divergent_show_runs_verbatim(tmp_path, monkeypatch):
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held

        def create_with_divergent_show_runs(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            readback = dict(result.readback)
            capture = dict(result.readback["raw_capture_v1"])
            show = dict(capture["show"])
            show["runs"] = [{"opaque_show_run": True}]
            capture["show"] = show
            readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_with_divergent_show_runs
        result = coordinator.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "held", result
        operation = next(op for op in store.read_scope(coordinator.scope)["operations"]
                         if op.key == result["operation_key"])
        capture = operation.readback["raw_capture_v1"]
        assert capture["show"]["runs"] == ({"opaque_show_run": True},)
        assert capture["runs"] == ()
        assert [member for member in store.read_scope(coordinator.scope)["members"]
                if member.role == "implementation"]
    finally:
        store.close()


@pytest.mark.parametrize("capture_damage", ("missing", "malformed", "disagrees"))
def test_active_piece_create_is_not_acknowledged_without_exact_raw_capture(
    tmp_path, monkeypatch, capture_damage,
):
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held

        def create_without_exact_capture(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            store.begin_effect_attempt(coordinator.scope, action.key)
            readback = dict(result.readback)
            if capture_damage == "missing":
                readback.pop("raw_capture_v1", None)
            elif capture_damage == "malformed":
                readback["raw_capture_v1"] = {"kind": "wrong", "show": {}, "runs": []}
            else:
                capture = dict(result.readback["raw_capture_v1"])
                show = dict(capture["show"])
                task = dict(show["task"])
                task["status"] = "ready"
                show["task"] = task
                capture["show"] = show
                readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_without_exact_capture
        members_before = tuple(store.read_scope(coordinator.scope)["members"])
        budgets_before = tuple(store.read_scope(coordinator.scope)["budget_events"])
        result = coordinator.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "partial", result
        assert "raw" in result["reason"] or "capture" in result["reason"], result
        state = store.read_scope(coordinator.scope)
        assert tuple(state["members"]) == members_before
        assert tuple(state["budget_events"]) == budgets_before
        operation = next(op for op in state["operations"] if op.key == result["operation_key"])
        assert operation.phase == "unknown"
        assert operation.outcome == "ambiguous"
        assert operation.readback is None
    finally:
        store.close()


def test_inspection_reports_concurrent_pause_as_uncertain_without_writes(tmp_path, monkeypatch):
    from local_first_orchestrator.contracts import PauseIntent

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        rows_after_interleaving = []

        def base_reader(_scope, task_ids):
            return {task_id: _raw(cards[task_id]) for task_id in task_ids}

        def pause_after_read(scope, task_ids):
            raw = base_reader(scope, task_ids)
            store.set_operator_intent(PauseIntent(scope, "operator", 1, True, False))
            rows_after_interleaving[:] = _database_rows(store)
            return raw

        board.read_accepted_active_tranche_raw_cards = pause_after_read
        result = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert result["outcome"] == "conflict", result
        assert result["observation_uncertain"] is True
        assert result["proven_applied_edges"] == ()
        assert result["no_effect_authority"] is True
        assert _database_rows(store) == tuple(rows_after_interleaving)
    finally:
        store.close()


def test_coordinator_inspection_reads_complete_tranche_without_store_or_native_writes(tmp_path, monkeypatch):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []

        def complete_raw(scope, task_ids):
            calls.append(tuple(task_ids))
            return {task_id: _raw(cards[task_id]) for task_id in task_ids}

        board.read_accepted_active_tranche_raw_cards = complete_raw
        before = _database_rows(store)
        result = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert result["outcome"] == "reconciliation_required", result
        assert result["missing_declared_edges"] == (("TK-A", "TK-B"),)
        assert result["proven_applied_edges"] == ()
        assert result["no_effect_authority"] is True and result["observation_is_not_atomic"] is True
        assert calls == [("native-TK-A", "native-TK-B")]
        assert _database_rows(store) == before
    finally:
        store.close()


def test_zero_edge_inspection_requires_same_trusted_request_observer_fence(tmp_path, monkeypatch):
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        coordinator.planning_observer = None
        board.read_accepted_active_tranche_raw_cards = lambda scope, task_ids: {task_id: _raw(cards[task_id]) for task_id in task_ids}
        before = _database_rows(store)
        with pytest.raises(ValueError, match="trusted planning observer"):
            coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("cancellation_requested", (False, True))
def test_coordinator_inspection_honors_pause_without_native_read_or_write(
    tmp_path, monkeypatch, cancellation_requested,
):
    from local_first_orchestrator.contracts import PauseIntent
    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        store.set_operator_intent(PauseIntent(
            coordinator.scope, "operator", 1, True, cancellation_requested))
        board.read_accepted_active_tranche_raw_cards = lambda *_: (_ for _ in ()).throw(AssertionError("must not read"))
        coordinator.planning_observer = lambda *_: (_ for _ in ()).throw(AssertionError("must not observe"))
        before = _database_rows(store)
        cards_before = dict(cards)
        result = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert result["outcome"] == "paused" and result["no_effect_authority"] is True
        assert result["declared_edges"] == (("TK-A", "TK-B"),)
        assert result["missing_declared_edges"] == result["declared_edges"]
        assert result["proven_applied_edges"] == ()
        assert result["pending_or_unknown_edges"] == ()
        assert result["conflicts"] == ()
        assert result["opaque_fields"] == ()
        assert result["observation_is_not_atomic"] is True
        with pytest.raises(TypeError):
            result["outcome"] = "approved"
        assert _database_rows(store) == before
        assert cards == cards_before
    finally:
        store.close()


@pytest.mark.parametrize("failure", ("show", "runs", "unavailable", "missing_reader", "later_card"))
def test_coordinator_inspection_reports_raw_reader_failures_without_writes(
    tmp_path, monkeypatch, failure,
):
    import sys
    from local_first_orchestrator.hermes_board import HermesBoardAdapter

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        calls = []
        adapter = HermesBoardAdapter(
            board=coordinator.scope["board_id"], anchor_task_id=coordinator.scope["anchor_task_id"],
            executable=sys.executable, hermes_home=tmp_path / "unused-home",
            kanban_home=tmp_path / "unused-kanban",
            managed_member_lookup=lambda scope, task_id: scope == coordinator.scope and task_id in cards,
        )

        def invoke(command, task_id, *_args, **_kwargs):
            calls.append((command, task_id))
            assert command in {"show", "runs"}
            if failure == "unavailable":
                raise OSError("observation unavailable")
            if failure == "show" and command == "show":
                return []
            if failure == "runs" and command == "runs":
                return {"not": "runs"}
            if failure == "later_card" and task_id == "native-TK-B":
                raise TimeoutError("second observation unavailable")
            return _raw(cards[task_id]) if command == "show" else []

        monkeypatch.setattr(adapter, "_invoke", invoke)
        board.read_accepted_active_tranche_raw_cards = (
            None if failure == "missing_reader" else adapter.read_accepted_active_tranche_raw_cards)
        before = _database_rows(store)
        cards_before = dict(cards)
        result = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert result["outcome"] == "conflict"
        assert result["declared_edges"] == (("TK-A", "TK-B"),)
        assert result["missing_declared_edges"] == result["declared_edges"]
        assert result["proven_applied_edges"] == ()
        assert result["pending_or_unknown_edges"] == ()
        assert result["opaque_fields"] == ()
        assert result["conflicts"][0]["kind"] == "raw_observation_conflict"
        assert result["conflicts"][0]["reason"]
        assert len(result["conflicts"][0]["reason"]) <= 384
        assert result["no_effect_authority"] is True
        assert result["observation_is_not_atomic"] is True
        with pytest.raises(TypeError):
            result["conflicts"][0]["kind"] = "approved"
        assert _database_rows(store) == before
        assert cards == cards_before
        if failure == "missing_reader":
            assert calls == []
        elif failure == "later_card":
            assert calls == [("show", "native-TK-A"), ("runs", "native-TK-A"),
                             ("show", "native-TK-B")]
        else:
            assert calls
    finally:
        store.close()


def test_inspector_preserves_stable_opaque_raw_fields_captured_with_create_receipt(tmp_path, monkeypatch):
    """A stable unknown public-show field is compared to its creation-time capture."""
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held
        opaque = {"extension": {"retained": ["v1", 7]}}

        def create_with_raw_capture(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            if result.outcome not in {"verified", "no-op"} or result.readback is None:
                return result
            task_id = "native-" + action.target["ticket_id"]
            show = _raw(cards[task_id])
            show["opaque_native_extension"] = opaque
            readback = dict(result.readback)
            readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1",
                                          "show": show, "runs": show["runs"]}
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_with_raw_capture

        def raw_read(_scope, task_ids):
            result = {}
            for task_id in task_ids:
                current = _raw(cards[task_id])
                current["opaque_native_extension"] = opaque
                result[task_id] = current
            return result

        board.read_accepted_active_tranche_raw_cards = raw_read
        _seed_two(coordinator)
        before = _database_rows(store)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert report["outcome"] == "reconciliation_required", report
        assert {entry["task_id"]: entry["fields"] for entry in report["opaque_fields"]} == {
            "native-TK-A": ("comments", "latest_summary", "opaque_native_extension"),
            "native-TK-B": ("comments", "latest_summary", "opaque_native_extension"),
        }
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("drift", (None, "show_runs", "endpoint_runs"))
def test_inspection_preserves_and_checks_divergent_show_runs_and_endpoint(
    tmp_path, monkeypatch, drift,
):
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held

        def create_with_divergent_baseline(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            readback = dict(result.readback)
            capture = dict(result.readback["raw_capture_v1"])
            show = dict(capture["show"])
            show["runs"] = [{"show_only": True}]
            capture["show"] = show
            readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_with_divergent_baseline
        _seed_two(coordinator)

        def raw_reader(_scope, task_ids):
            result = {}
            for task_id in task_ids:
                card = _raw(cards[task_id])
                card.pop("runs", None)
                if drift == "endpoint_runs":
                    endpoint_runs = [{"endpoint_only": True}]
                else:
                    endpoint_runs = []
                show_runs = [{"changed": True}] if drift == "show_runs" else [{"show_only": True}]
                result[task_id] = {**card, "runs": endpoint_runs,
                                   "show_has_runs": True, "show_runs": show_runs}
            return result

        board.read_accepted_active_tranche_raw_cards = raw_reader
        before = _database_rows(store)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert _database_rows(store) == before
        if drift is None:
            assert report["outcome"] == "reconciliation_required", report
            assert report["conflicts"] == ()
        else:
            assert report["outcome"] == "conflict", report
            assert report["proven_applied_edges"] == ()
    finally:
        store.close()


@pytest.mark.parametrize("baseline_has_runs,observed_has_runs,expected", [
    (False, False, "reconciliation_required"),
    (False, True, "conflict"),
    (True, False, "conflict"),
    (True, True, "reconciliation_required"),
])
def test_inspection_preserves_absent_vs_empty_show_runs(tmp_path, monkeypatch,
                                                        baseline_has_runs, observed_has_runs, expected):
    from local_first_orchestrator.contracts import ActionResult

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        original_create = board.create_held

        def create_with_presence(action, *, title, body, **kwargs):
            result = original_create(action, title=title, body=body, **kwargs)
            readback = dict(result.readback)
            capture = dict(result.readback["raw_capture_v1"])
            show = dict(capture["show"])
            if baseline_has_runs:
                show["runs"] = []
            else:
                show.pop("runs", None)
            capture["show"] = show
            readback["raw_capture_v1"] = capture
            return ActionResult(result.action_key, result.outcome, result.details, readback)

        board.create_held = create_with_presence
        _seed_two(coordinator)
        def raw_read(_scope, task_ids):
            observed = {}
            for task_id in task_ids:
                card = _raw(cards[task_id])
                if observed_has_runs:
                    card["runs"] = []
                else:
                    card.pop("runs", None)
                observed[task_id] = {**card, "runs": [], "show_has_runs": observed_has_runs,
                                     "show_runs": card.get("runs") if observed_has_runs else None}
            return observed

        board.read_accepted_active_tranche_raw_cards = raw_read
        before = _database_rows(store)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert report["outcome"] == expected, report
        assert _database_rows(store) == before
    finally:
        store.close()


def test_parent_native_inspection_of_three_held_cards_is_read_only(tmp_path, native_fixture, monkeypatch):
    """Parent-only disposable fixture: materialization mutates; inspection never does."""
    from tests.test_m4_active_piece_preparation import _accepted_plan
    from tests.test_m4_native_second_edge_link_characterization import _three_card_proposal

    (_board, _anchor, _workspace, adapter, _membership, cli, coordinator, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch,
                                             proposal_factory=_three_card_proposal)
    try:
        prepared = coordinator.prepare_active_tranche(accepted["plan_id"], request_id=request_id)
        assert prepared["outcome"] == "held" and prepared["total"] == 3, prepared
        before_store = _database_rows(store)
        before_native = cli("list", "--json").stdout
        calls = []
        original = adapter._invoke
        monkeypatch.setattr(adapter, "_invoke", lambda *args, **kwargs: (
            calls.append(args), original(*args, **kwargs))[1])
        report = coordinator.inspect_accepted_active_tranche_dependencies(
            accepted["plan_id"], request_id=request_id)
        assert report["outcome"] == "reconciliation_required", report
        assert report["missing_declared_edges"] == (("TK-A", "TK-B"), ("TK-A", "TK-C"), ("TK-B", "TK-C"))
        assert all(args[0] in {"show", "runs"} for args in calls)
        assert _database_rows(store) == before_store
        assert cli("list", "--json").stdout == before_native
    finally:
        store.close()


def _serial_three_card_proposal(request):
    """Use the existing three-held-card contract, narrowed to A -> B -> C."""
    import dataclasses

    from local_first_orchestrator.decomposition_planner import PlanProposal
    from tests.test_m4_native_second_edge_link_characterization import _three_card_proposal

    original = _three_card_proposal(request)
    tranche = original.plan.tranches[0]
    ticket_a, ticket_b, ticket_c = tranche.tickets
    ticket_c = dataclasses.replace(ticket_c, dependencies=(ticket_b.ticket_id,))
    plan = dataclasses.replace(
        original.plan,
        tranches=(dataclasses.replace(tranche, tickets=(ticket_a, ticket_b, ticket_c)),),
    )
    return PlanProposal(original.request_identity, plan, original.tranche_semantics)


def test_parent_native_generalized_v2_serial_dag_execution_and_applied_replay(tmp_path, native_fixture, monkeypatch):
    """Parent-only: public v2 APIs prove A->B->C and read-only applied replay."""
    from tests.test_m4_active_piece_preparation import _accepted_plan

    (_board, _anchor, _workspace, adapter, _membership, cli, coordinator, store, scope,
     accepted, request_id) = _accepted_plan(
        tmp_path, native_fixture, monkeypatch, proposal_factory=_serial_three_card_proposal,
    )
    try:
        held = coordinator.prepare_active_tranche(accepted["plan_id"], request_id=request_id)
        assert held["outcome"] == "held" and held["completed"] == held["total"] == 3, held
        assert tuple(piece["ticket_id"] for piece in held["pieces"]) == ("TK-A", "TK-B", "TK-C")
        state = store.read_scope(scope)
        task_for = {}
        for piece in held["pieces"]:
            operation = next(item for item in state["operations"] if item.key == piece["operation_key"])
            task_for[operation.target["ticket_id"]] = piece["task_id"]
        assert set(task_for) == {"TK-A", "TK-B", "TK-C"}
        assert len(set(task_for.values())) == 3
        members_before = tuple(state["members"])
        budgets_before = tuple(state["budget_events"])

        def raw_graph():
            raw = {ticket: json.loads(cli("show", task_id, "--json").stdout)
                   for ticket, task_id in task_for.items()}
            assert all(card["task"]["status"] == "blocked" and card["runs"] == []
                       for card in raw.values())
            return ({ticket: tuple(card["parents"]) for ticket, card in raw.items()},
                    {ticket: tuple(card["children"]) for ticket, card in raw.items()})

        assert raw_graph() == ({"TK-A": (), "TK-B": (), "TK-C": ()},
                               {"TK-A": (), "TK-B": (), "TK-C": ()})
        native_link_calls, lock_assertions = [], []
        original_invoke = adapter._invoke
        original_lock_assertion = adapter.create_lock_assertion

        def invoke(*args, **kwargs):
            if args and args[0] == "link":
                native_link_calls.append(args[1:])
            return original_invoke(*args, **kwargs)

        def assert_singleton_lock(*args, **kwargs):
            lock_assertions.append((args, kwargs))
            return original_lock_assertion(*args, **kwargs)

        monkeypatch.setattr(adapter, "_invoke", invoke)
        monkeypatch.setattr(adapter, "create_lock_assertion", assert_singleton_lock)

        first_pending = coordinator.prepare_accepted_dependency_link(
            accepted["plan_id"], "TK-B", "TK-A", request_id=request_id,
        )
        assert first_pending["outcome"] == "pending"
        assert first_pending["target"]["kind"] == "accepted_active_tranche_native_link_v2"
        first = coordinator.execute_accepted_dependency_link(
            accepted["plan_id"], "TK-B", "TK-A", request_id=request_id,
        )
        assert first == {"outcome": "linked", "operation_key": first_pending["operation_key"], "actions_attempted": 1}

        second_pending = coordinator.prepare_accepted_dependency_link(
            accepted["plan_id"], "TK-C", "TK-B", request_id=request_id,
        )
        assert second_pending["outcome"] == "pending"
        second = coordinator.execute_accepted_dependency_link(
            accepted["plan_id"], "TK-C", "TK-B", request_id=request_id,
        )
        assert second == {"outcome": "linked", "operation_key": second_pending["operation_key"], "actions_attempted": 1}
        assert native_link_calls == [(task_for["TK-A"], task_for["TK-B"]),
                                     (task_for["TK-B"], task_for["TK-C"])]
        # v2 asserts the configured singleton immediately before both native calls.
        assert len(lock_assertions) == 4
        assert raw_graph() == (
            {"TK-A": (), "TK-B": (task_for["TK-A"],), "TK-C": (task_for["TK-B"],)},
            {"TK-A": (task_for["TK-B"],), "TK-B": (task_for["TK-C"],), "TK-C": ()},
        )
        state = store.read_scope(scope)
        assert tuple(state["members"]) == members_before
        assert tuple(state["budget_events"]) == budgets_before
        applied = [item for item in state["operations"] if item.key in {
            first_pending["operation_key"], second_pending["operation_key"]}]
        assert len(applied) == 2 and all(item.phase == "applied" and item.outcome == "verified" for item in applied)
        assert all(item.readback["kind"] == "accepted_active_tranche_native_link_readback_v2"
                   and item.readback["time_window"]["started"] <= item.readback["time_window"]["ended"]
                   and item.readback["time_window"]["started"] <= item.readback["observed_cards"][
                       task_for[item.readback["edge"][1]]]["events"][-1]["created_at"] <= item.readback["time_window"]["ended"]
                   for item in applied)

        before_replay_store = _database_rows(store)
        before_replay_links, before_replay_locks = tuple(native_link_calls), tuple(lock_assertions)
        replay = coordinator.execute_accepted_dependency_link(
            accepted["plan_id"], "TK-C", "TK-B", request_id=request_id,
        )
        assert replay == {"outcome": "linked", "operation_key": second_pending["operation_key"], "actions_attempted": 0}
        assert tuple(native_link_calls) == before_replay_links
        assert tuple(lock_assertions) == before_replay_locks
        assert _database_rows(store) == before_replay_store
        assert tuple(store.read_scope(scope)["members"]) == members_before
        assert tuple(store.read_scope(scope)["budget_events"]) == budgets_before
    finally:
        store.close()
