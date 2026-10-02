from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def native_fixture(tmp_path):
    """Use the pinned Hermes CLI fixture; skip rather than simulate production."""
    from tests.test_m4_planner_creation import native_fixture as fixture
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to pinned Hermes CLI")
    return fixture.__wrapped__(tmp_path)

from local_first_orchestrator.contracts import ManagedMember
from local_first_orchestrator.decomposition_planner import request_payload
from local_first_orchestrator.planning_coordinator import (
    ActiveTrancheRoute,
    _canonical_digest,
    first_active_tranche_materialization,
    reconstruct_evidence,
)
from tests.test_m4_active_piece_dependency_authority import _batch_fixture, _database_rows, _seed_two
from tests.test_m4_plan_acceptance import _pure_acceptance_fixture
from tests.test_m4_plan_evidence import SCOPE, proposal


def _raw(snapshot):
    task = dict(snapshot.native_task)
    if "workspace_path" not in task and task.get("workspace", "").startswith("dir:"):
        task["workspace_path"] = task["workspace"][4:]
    return {"task": task, "parents": [item["id"] for item in snapshot.parents],
            "children": [], "runs": list(snapshot.runs), "events": list(snapshot.events),
            "comments": list(snapshot.comments), "latest_summary": None,
            **({"attachments": list(snapshot.attachments)} if snapshot.attachments else {})}


def _snapshot_for_raw(raw):
    from local_first_orchestrator.hermes_board import HermesBoardAdapter
    data = {"native_task": raw["task"], "parents": [{"id": item} for item in raw.get("parents", [])],
            "runs": [], "comments": raw.get("comments", []), "events": raw.get("events", []),
            "attachments": raw.get("attachments", [])}
    return {**data, "observed_at": "unit", "digest": HermesBoardAdapter._digest(data)}


def test_genuine_zero_edge_coordinator_inspection_rechecks_full_context_without_writes(tmp_path, monkeypatch):
    """One accepted active piece covers all criteria and genuinely declares no edges."""
    # Replace the standard evidence before constructing its accepted fixture.
    import tests.test_m4_plan_acceptance as acceptance
    original = acceptance.evidence
    store = None
    try:

        def one_piece_evidence():
            base = original()
            request, plan = reconstruct_evidence(base)
            sole = dataclasses.replace(plan.plan.tranches[0].tickets[0], criterion_ids=("AC-1", "AC-2"))
            single = dataclasses.replace(plan.plan, tranches=(dataclasses.replace(
                plan.plan.tranches[0], tickets=(sole,), criterion_ids=("AC-1", "AC-2")),),
                criterion_coverage={"AC-1": ("TK-A",), "AC-2": ("TK-A",)})
            from local_first_orchestrator.decomposition_planner import PlanProposal
            from local_first_orchestrator.planning_coordinator import evidence_payload
            return evidence_payload(request, PlanProposal(request.identity, single, plan.tranche_semantics[:1]),
                planner_task_id=base["planner"]["task_id"], planner_run_id=base["planner"]["run_id"],
                planner_session_id=base["planner"]["session_id"], planner_profile=base["planner"]["profile"])

        monkeypatch.setattr(acceptance, "evidence", one_piece_evidence)
        # Construct an independent accepted fixture under the replacement evidence.
        coordinator, store, board, _observed = _pure_acceptance_fixture(tmp_path)
        coordinator.accept_validated_plan("plan-1")
        created = {}
        from local_first_orchestrator.contracts import ActionResult, BoardSnapshot
        from local_first_orchestrator.hermes_board import HermesBoardAdapter
        def held(task_id, *, title, body):
            native = {"id": task_id, "status": "blocked", "assignee": "implementer",
                      "title": title, "body": body, "workspace": f"dir:{tmp_path}"}
            digest = HermesBoardAdapter._digest({"native_task": native, "parents": [], "runs": [],
                                                 "comments": [], "events": [], "attachments": []})
            return BoardSnapshot(native, (), (), (), (), (), "unit", digest)
        board.read_task = lambda task_id: (BoardSnapshot({"id": "anchor-A", "status": "ready"}, (), (), (), (), (), "unit", "anchor")
                                            if task_id == "anchor-A" else created[task_id])
        def create_held(action, *, title, body, **_kwargs):
            snapshot = held("native-" + action.target["ticket_id"], title=title, body=body)
            created[snapshot.native_task["id"]] = snapshot
            readback = snapshot.to_dict()
            readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1",
                                          "show": _raw(snapshot), "runs": []}
            return ActionResult(action.key, "verified", "created", readback)
        board.create_held = create_held
        board.verify_effect = lambda action: ActionResult(action.key, "verified", "current", created["native-" + action.target["ticket_id"]].to_dict())
        prepared = coordinator.prepare_active_piece("plan-1", "TK-A")
        assert prepared["outcome"] == "held", prepared
        board.read_accepted_active_tranche_raw_cards = lambda _scope, task_ids: {task_id: _raw(created[task_id]) for task_id in task_ids}
        before = _database_rows(store)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert report["declared_edges"] == ()
        assert report["proven_applied_edges"] == ()
        assert report["missing_declared_edges"] == ()
        assert report["outcome"] == "reconciliation_required"
        assert report["no_effect_authority"] is True and report["observation_is_not_atomic"] is True
        assert _database_rows(store) == before
    finally:
        if store is not None:
            store.close()

def test_future_managed_member_association_is_rejected_before_native_inspection(tmp_path):
    coordinator, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        coordinator.accept_validated_plan("plan-1")
        # First register the actual active managed piece so the future member is
        # the only out-of-scope association, not a substitute for active evidence.
        from local_first_orchestrator.contracts import ActionResult, BoardSnapshot
        from local_first_orchestrator.hermes_board import HermesBoardAdapter
        created = {}
        def create_held(action, *, title, body, **_kwargs):
            task_id = "native-" + action.target["ticket_id"]
            native = {"id": task_id, "status": "blocked", "assignee": "implementer",
                      "title": title, "body": body, "workspace": f"dir:{tmp_path}"}
            snapshot = BoardSnapshot(native, (), (), (), (), (), "unit", HermesBoardAdapter._digest(
                {"native_task": native, "parents": [], "runs": [], "comments": [], "events": [], "attachments": []}))
            created[task_id] = snapshot
            readback = snapshot.to_dict()
            readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1",
                                          "show": _raw(snapshot), "runs": []}
            return ActionResult(action.key, "verified", "created", readback)
        board.read_task = lambda task_id: (BoardSnapshot({"id": "anchor-A", "status": "ready"}, (), (), (), (), (), "unit", "anchor")
                                            if task_id == "anchor-A" else created[task_id])
        board.create_held = create_held
        board.verify_effect = lambda action: ActionResult(action.key, "verified", "current", created["native-" + action.target["ticket_id"]].to_dict())
        prepared = coordinator.prepare_active_piece("plan-1", "TK-A")
        assert prepared["outcome"] == "held", prepared
        evidence = store.read_plan(SCOPE, "plan-1")
        token = store.read_accepted_plan(SCOPE, "plan-1")
        route = ActiveTrancheRoute(token["route"]["implementation_profile"], token["route"]["workspace"])
        request, accepted = reconstruct_evidence(evidence)
        future = accepted.plan.tranches[1].tickets[0]
        association = "active-tranche-piece:" + _canonical_digest({
            "kind": "active_tranche_piece_v1", "board_id": request.board_id,
            "anchor_task_id": request.anchor_id, "plan_id": accepted.plan.plan_id,
            "request_identity": request.identity, "proposal_hash": accepted.proposal_hash,
            "plan_contract_hash": accepted.plan.contract_hash,
            "tranche_id": accepted.plan.tranches[1].tranche_id, "tranche_ordinal": 1,
            "ticket_id": future.ticket_id, "ticket_contract_hash": future.contract_hash,
        })
        store.register_member(ManagedMember(SCOPE["board_id"], SCOPE["anchor_task_id"], "native-future", "implementation", 0, (), association))
        board.read_task = lambda *_: pytest.fail("future membership must fail before native reads")
        before = _database_rows(store)
        with pytest.raises(ValueError, match="future managed member"):
            coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert _database_rows(store) == before
    finally:
        store.close()


def test_inspection_rejects_nonempty_frozen_creation_children_before_native_read(tmp_path, monkeypatch):
    from local_first_orchestrator.contracts import OperationIntent

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        original_read_scope = store.read_scope

        def tampered_scope(scope):
            state = original_read_scope(scope)
            operations = []
            for operation in state["operations"]:
                if operation.effect == "create_held" and operation.target.get("ticket_id") == "TK-A":
                    readback = coordinator._plain_link_value(operation.readback)
                    readback["raw_capture_v1"]["show"]["children"] = ["unmanaged-child"]
                    operation = OperationIntent(**{**operation.to_dict(), "readback": readback})
                operations.append(operation)
            state["operations"] = tuple(operations)
            return state

        store.read_scope = tampered_scope
        board.read_accepted_active_tranche_raw_cards = lambda *_: pytest.fail(
            "malformed frozen creation capture must fail before fresh native reads")
        before = _database_rows(store)
        result = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert result["outcome"] == "conflict", result
        assert result["proven_applied_edges"] == ()
        assert result["no_effect_authority"] is True
        assert _database_rows(store) == before
    finally:
        store.close()


def test_first_link_preparation_refuses_legacy_ack_without_raw_creation_capture(tmp_path, monkeypatch):
    from local_first_orchestrator.contracts import OperationIntent

    coordinator, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        original_read_scope = store.read_scope

        def legacy_scope(scope):
            state = original_read_scope(scope)
            operations = []
            for operation in state["operations"]:
                if (operation.effect == "create_held"
                        and operation.target.get("kind") in {
                            "accepted_active_tranche_piece_v1", "accepted_active_tranche_piece_v2"}
                        and operation.readback is not None):
                    readback = dict(operation.readback)
                    readback.pop("raw_capture_v1", None)
                    operation = OperationIntent(**{**operation.to_dict(), "readback": readback})
                operations.append(operation)
            state["operations"] = tuple(operations)
            return state

        store.read_scope = legacy_scope
        board.validate_accepted_piece_frozen_receipt = lambda *args, **kwargs: None
        board.link = lambda *args, **kwargs: pytest.fail("missing raw creation baseline must fail before link I/O")
        before = _database_rows(store)
        with pytest.raises(ValueError, match="raw creation baseline"):
            coordinator.execute_accepted_first_link("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("source_target_damage", (None, "list", "dict", "missing_kind", "unknown_kind"))
def test_malformed_applied_link_is_a_diagnostic_conflict_without_native_edge_or_writes(
    tmp_path, monkeypatch, source_target_damage,
):
    from local_first_orchestrator.contracts import OperationIntent

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        authority = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        source_id, child_id = authority["source_task_id"], authority["target_task_id"]
        source_raw, child_raw = _raw(cards[source_id]), _raw(cards[child_id])
        target = {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": source_id,
                  "child_task_id": child_id, "operation_key": authority["operation_key"]}
        if source_target_damage == "list":
            target["source_task_id"] = []
        elif source_target_damage == "dict":
            target["source_task_id"] = {"unhashable": True}
        elif source_target_damage == "missing_kind":
            target.pop("kind")
        elif source_target_damage == "unknown_kind":
            target["kind"] = "future_native_link_v3"
        child_receipt = next(item for item in authority["frozen_create_receipts"]
                             if item["task_id"] == child_id)
        before = {"kind": "accepted_active_tranche_native_link_prebarrier_v1",
                  "authority": dict(authority), "source_raw": source_raw, "child_raw": child_raw,
                  "expected_source_raw": source_raw,
                  "source_snapshot": _snapshot_for_raw(source_raw),
                  "child_snapshot": _snapshot_for_raw(child_raw)}
        store.reserve_operation(OperationIntent(
            authority["operation_key"], SCOPE, target, "link", child_receipt["digest"], before,
            "verified", {"kind": "accepted_active_tranche_native_link_readback_v1",
                         "source": source_raw, "child": child_raw},
            {"reconcile_only": True}, "applied"))
        board.read_accepted_active_tranche_raw_cards = lambda _scope, task_ids: {
            task_id: _raw(cards[task_id]) for task_id in task_ids}
        before_rows = _database_rows(store)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert report["outcome"] == "conflict", report
        assert report["proven_applied_edges"] == ()
        assert any(item["kind"] == "applied_receipt_conflict" for item in report["conflicts"]), report
        assert report["no_effect_authority"] is True
        assert _database_rows(store) == before_rows
    finally:
        store.close()


@pytest.mark.parametrize(("before_kind", "receipt_damage"), (
    ("accepted_active_tranche_native_link_prebarrier_v2", None),
    ("accepted_active_tranche_native_link_prebarrier_v1", None), ("invalid", None), (None, None),
    ("accepted_active_tranche_native_link_prebarrier_v2", "missing_source_snapshot"),
    ("accepted_active_tranche_native_link_prebarrier_v2", "missing_child_snapshot"),
    ("accepted_active_tranche_native_link_prebarrier_v2", "missing_transition"),
    ("accepted_active_tranche_native_link_prebarrier_v2", "transition_event"),
    ("accepted_active_tranche_native_link_prebarrier_v2", "source_snapshot_task"),
))
def test_pure_inspector_applied_receipt_rejects_raw_readback_drift_without_writes(
    tmp_path, monkeypatch, before_kind, receipt_damage,
):
    """The read-only inspector enforces the production prebarrier validator too."""
    from local_first_orchestrator.contracts import OperationIntent

    coordinator, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(coordinator)
        authority = coordinator.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        source_id, child_id = authority["source_task_id"], authority["target_task_id"]
        source_before, child_before = _raw(cards[source_id]), _raw(cards[child_id])
        source_after, child_after = copy.deepcopy(source_before), copy.deepcopy(child_before)
        source_after["children"] = [child_id]
        child_after["parents"] = [source_id]
        child_after["events"].append({"created_at": 1, "kind": "linked",
                                      "payload": {"parent": source_id, "child": child_id}, "run_id": None})
        target = {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": source_id,
                  "child_task_id": child_id, "operation_key": authority["operation_key"]}
        from local_first_orchestrator.hermes_board import validate_accepted_first_link_transition
        before = {"authority": dict(authority), "source_raw": source_before,
                  "child_raw": child_before, "expected_source_raw": source_after,
                  "source_snapshot": _snapshot_for_raw(source_before),
                  "child_snapshot": _snapshot_for_raw(child_before)}
        if before_kind is not None:
            before["kind"] = before_kind
        if before_kind == "accepted_active_tranche_native_link_prebarrier_v2":
            before.update(request_id=None, captured_clock_seconds=1, event_lower_bound_seconds=1)
        transition = validate_accepted_first_link_transition(
            source_before, child_before, source_after, child_after,
            source_id=source_id, child_id=child_id,
            command_started_seconds=1, command_ended_seconds=1)
        readback = {"kind": "accepted_active_tranche_native_link_readback_v1",
                    "source": source_after, "child": child_after,
                    "source_snapshot": _snapshot_for_raw(source_after),
                    "child_snapshot": _snapshot_for_raw(child_after),
                    "time_window": {"started": 1, "ended": 1},
                    "transition": {"source_id": transition.source_id,
                                   "child_id": transition.child_id,
                                   "event": dict(transition.event)}}
        if receipt_damage == "missing_source_snapshot":
            readback.pop("source_snapshot")
        elif receipt_damage == "missing_child_snapshot":
            readback.pop("child_snapshot")
        elif receipt_damage == "missing_transition":
            readback.pop("transition")
        elif receipt_damage == "transition_event":
            readback["transition"].pop("event")
        elif receipt_damage == "source_snapshot_task":
            readback["source_snapshot"]["native_task"]["status"] = "ready"
        child_receipt = next(receipt for receipt in authority["frozen_create_receipts"]
                             if receipt["task_id"] == child_id)
        operation = store.reserve_operation(OperationIntent(
            authority["operation_key"], SCOPE, target, "link", child_receipt["digest"], before,
            "verified", readback, {"reconcile_only": True}, "applied"))
        assert operation.phase == "applied"
        def raw_read(_scope, task_ids):
            out = {}
            for task_id in task_ids:
                card = copy.deepcopy(source_after if task_id == source_id else child_after)
                out[task_id] = {**card, "show_has_runs": True, "show_runs": card.get("runs")}
            return out
        board.read_accepted_active_tranche_raw_cards = raw_read
        before_rows = _database_rows(store)
        baseline = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        if (before_kind != "accepted_active_tranche_native_link_prebarrier_v2"
                or receipt_damage is not None):
            assert baseline["outcome"] == "conflict", baseline
            assert baseline["proven_applied_edges"] == ()
            assert baseline["no_effect_authority"] is True
            assert _database_rows(store) == before_rows
            return
        assert baseline["proven_applied_edges"] == (("TK-A", "TK-B"),), baseline
        before_dump, before_state = tuple(store.connection.iterdump()), store.read_scope(SCOPE)
        original_read_scope = store.read_scope

        def drifted_scope(scope):
            state = original_read_scope(scope)
            changed = []
            for item in state["operations"]:
                if item.key != authority["operation_key"]:
                    changed.append(item)
                    continue
                wire = item.to_dict()
                wire["readback"]["source"]["comments"] = [{"body": "drift"}]
                changed.append(OperationIntent.from_dict(wire))
            return {**state, "operations": tuple(changed)}

        monkeypatch.setattr(store, "read_scope", drifted_scope)
        report = coordinator.inspect_accepted_active_tranche_dependencies("plan-1")
        assert report["outcome"] == "conflict", report
        assert report["proven_applied_edges"] == ()
        assert report["no_effect_authority"] is True
        assert tuple(store.connection.iterdump()) == before_dump
        current = original_read_scope(SCOPE)
        assert tuple(current["members"]) == tuple(before_state["members"])
        assert tuple(current["budget_events"]) == tuple(before_state["budget_events"])
    finally:
        store.close()


@pytest.mark.parametrize(
    ("case", "mutate"),
    (
        ("authority_identity_request_root_route_mismatch",
         lambda wire: wire["before_evidence"]["authority"].update({"request_identity": "sha256:" + "0" * 64})),
        ("stable_operation_key_target_table_per_edge_hash_mismatch",
         lambda wire: wire["before_evidence"]["authority"]["frozen_create_receipts"][0].update({"operation_key": "create-drift"})),
        ("time_window_start_end_timestamp_outside_bound",
         lambda wire: wire["readback"]["time_window"].update({"started": wire["readback"]["time_window"]["ended"] + 1})),
        ("appended_event_payload_kind_runid_extra_missing",
         lambda wire: wire["readback"]["child"]["events"][-1].update({"run_id": "drift-run"})),
        ("raw_prefix_history_source_child_other_fields_difference",
         lambda wire: wire["readback"]["source"].update({"comments": [{"body": "drift"}]})),
        ("missing_none_acknowledgement", lambda wire: wire.update({"readback": None})),
        ("native_supported_marker_create_receipt_mismatch",
         lambda wire: wire["before_evidence"]["authority"]["frozen_create_receipts"][0]["readback"]["native_task"].update({"body": "drift"})),
        ("unsupported_malformed_verifier_receipt",
         lambda wire: wire["readback"].update({"kind": "unsupported-verifier-receipt"})),
    ),
)
def test_production_coordinator_inspector_rejects_applied_receipt_drift_read_only(
    tmp_path, native_fixture, monkeypatch, case, mutate,
):
    """Applied first-edge receipts are immutable evidence, not caller-consistent hints.

    The parent fixture creates the accepted planner plus two held active pieces,
    then establishes an actual native first edge through the production adapter.
    Each child case changes one persisted-contract boundary while live native
    topology remains the original valid edge.
    """
    from local_first_orchestrator.contracts import ActionResult, OperationIntent
    from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal

    (_board, _anchor, _workspace, adapter, _membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch,
                                             proposal_factory=_batch_proposal)
    try:
        plan_id = accepted["plan_id"]
        prepared = controller.prepare_active_tranche(plan_id, request_id=request_id)
        if prepared["outcome"] != "held":
            pytest.fail("active tranche preparation diagnostic: " + json.dumps(prepared, default=str))
        authority = controller.active_piece_dependency_authority(plan_id, "TK-B", "TK-A", request_id=request_id)
        assert all("raw_capture_v1" in receipt["readback"]
                   for receipt in authority["frozen_create_receipts"])
        baseline = controller.execute_accepted_first_link(plan_id, "TK-B", "TK-A", request_id=request_id)
        assert baseline == {"outcome": "linked", "operation_key": authority["operation_key"], "actions_attempted": 1}
        source_id, child_id = authority["source_task_id"], authority["target_task_id"]
        native_after_baseline = (json.loads(cli("show", source_id, "--json").stdout),
                                 json.loads(cli("show", child_id, "--json").stdout))
        baseline_report = controller.inspect_accepted_active_tranche_dependencies(
            plan_id, request_id=request_id)
        assert baseline_report["outcome"] == "reconciliation_required", baseline_report
        assert baseline_report["proven_applied_edges"] == (("TK-A", "TK-B"),), baseline_report
        state_after_baseline = store.read_scope(scope)
        members_after_baseline = tuple(state_after_baseline["members"])
        budgets_after_baseline = tuple(state_after_baseline["budget_events"])
        token_after_baseline = store.read_accepted_plan(scope, plan_id)
        evidence_after_baseline = store.read_plan(scope, plan_id)

        original_read_scope = store.read_scope
        original_invoke = adapter._invoke
        native_commands = []
        def capture(command, *args, **kwargs):
            native_commands.append((command, *args))
            return original_invoke(command, *args, **kwargs)
        monkeypatch.setattr(adapter, "_invoke", capture)

        def tampered_read_scope(requested_scope):
            state = original_read_scope(requested_scope)
            changed = []
            for operation in state["operations"]:
                if operation.key != authority["operation_key"]:
                    changed.append(operation)
                    continue
                wire = operation.to_dict()
                mutate(wire)
                changed.append(OperationIntent.from_dict(wire))
            return {**state, "operations": tuple(changed)}
        monkeypatch.setattr(store, "read_scope", tampered_read_scope)

        dump_after_injection = tuple(store.connection.iterdump())
        result = controller.inspect_accepted_active_tranche_dependencies(plan_id, request_id=request_id)

        assert result["outcome"] == "conflict", (case, result)
        assert result["proven_applied_edges"] == (), (case, result)
        assert result["no_effect_authority"] is True and result["observation_is_not_atomic"] is True
        assert tuple(store.connection.iterdump()) == dump_after_injection
        current_state = original_read_scope(scope)
        assert tuple(current_state["members"]) == members_after_baseline
        assert tuple(current_state["budget_events"]) == budgets_after_baseline
        assert store.read_accepted_plan(scope, plan_id) == token_after_baseline
        assert store.read_plan(scope, plan_id) == evidence_after_baseline
        assert (json.loads(cli("show", source_id, "--json").stdout),
                json.loads(cli("show", child_id, "--json").stdout)) == native_after_baseline
        assert not ({"link", "claim", "release", "unblock", "dispatch", "accept", "complete"}
                    & {command for command, *_ in native_commands})
    finally:
        store.close()
