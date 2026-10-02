from __future__ import annotations

import hashlib
import json
import dataclasses

import pytest

from local_first_orchestrator.contracts import ActionResult, BoardSnapshot
from local_first_orchestrator.planning_coordinator import evidence_payload
from tests.test_m4_active_piece_preparation import (
    _accepted_plan, _batch_proposal, native_fixture,
)
from tests.test_m4_plan_acceptance import _authority_rows, _pure_acceptance_fixture
from tests.test_m4_plan_evidence import SCOPE


def _database_rows(store):
    tables = tuple(row[0] for row in store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
    return tuple((table, tuple(tuple(row) for row in store.connection.execute("SELECT * FROM " + table)))
                 for table in tables)


def test_native_first_edge_authority_uses_production_adapter(
    tmp_path, native_fixture, monkeypatch,
):
    from tests.test_m4_active_piece_preparation import _piece_task_ids
    from tests.test_m4_plan_evidence import SCOPE

    (board, anchor, workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(
        tmp_path, native_fixture, monkeypatch, proposal_factory=_batch_proposal,
    )
    plan_id = accepted["plan_id"]
    try:
        before_tasks = _piece_task_ids(cli)
        tranche = controller.prepare_active_tranche(plan_id, request_id=request_id)
        assert tranche["outcome"] == "held", tranche
        assert tranche["completed"] == tranche["total"] == 2
        assert tuple(piece["ticket_id"] for piece in tranche["pieces"]) == ("TK-A", "TK-B")
        assert all(piece["outcome"] == "held" for piece in tranche["pieces"])
        assert sum(piece["actions_attempted"] for piece in tranche["pieces"]) == 2
        assert _piece_task_ids(cli) - before_tasks == {piece["task_id"] for piece in tranche["pieces"]}

        before_authority = _authority_rows(store)
        before_state = store.read_scope(scope)
        before = (before_authority, tuple(before_state["members"]),
                  tuple(before_state["operations"]), tuple(before_state["budget_events"]),
                  _piece_task_ids(cli))
        first = controller.active_piece_dependency_authority(
            plan_id, "TK-B", "TK-A", request_id=request_id,
        )
        second = controller.active_piece_dependency_authority(
            plan_id, "TK-B", "TK-A", request_id=request_id,
        )
        assert first["kind"] == "accepted_active_tranche_native_link_v1"
        assert first["source_ticket_id"] == "TK-A" and first["target_ticket_id"] == "TK-B"
        assert first["read_only_first_edge_only"] is True
        assert first["operation_key"] == second["operation_key"]
        assert first["frozen_create_receipts"] == second["frozen_create_receipts"]
        assert len(first["frozen_create_receipts"]) == 2
        for receipt in first["frozen_create_receipts"]:
            native = receipt["readback"]["native_task"]
            assert native["status"] == "blocked"
            assert native["assignee"] == "implementer"
            assert native["workspace_path"] == workspace
            assert not receipt["readback"]["parents"] and not receipt["readback"]["runs"]
        after_state = store.read_scope(scope)
        after = (_authority_rows(store), tuple(after_state["members"]),
                 tuple(after_state["operations"]), tuple(after_state["budget_events"]),
                 _piece_task_ids(cli))
        assert after == before

        task_b = next(piece["task_id"] for piece in tranche["pieces"]
                      if piece["ticket_id"] == "TK-B")
        cli("comment", task_b, "operator changed held card")
        before_drift_state = store.read_scope(scope)
        before_drift = (_authority_rows(store), tuple(before_drift_state["members"]),
                        tuple(before_drift_state["operations"]),
                        tuple(before_drift_state["budget_events"]), _piece_task_ids(cli))
        with pytest.raises(ValueError, match="fresh native card differs"):
            controller.active_piece_dependency_authority(
                plan_id, "TK-B", "TK-A", request_id=request_id,
            )
        after_drift_state = store.read_scope(scope)
        assert (_authority_rows(store), tuple(after_drift_state["members"]),
                tuple(after_drift_state["operations"]),
                tuple(after_drift_state["budget_events"]), _piece_task_ids(cli)) == before_drift
    finally:
        store.close()


def _batch_fixture(tmp_path, monkeypatch):
    import tests.test_m4_plan_acceptance as acceptance

    original = acceptance.evidence

    def batch_evidence():
        base = original()
        req = acceptance.base_request()
        return evidence_payload(req, _batch_proposal(req), planner_task_id=base["planner"]["task_id"],
            planner_run_id=base["planner"]["run_id"], planner_session_id=base["planner"]["session_id"],
            planner_profile=base["planner"]["profile"])

    monkeypatch.setattr(acceptance, "evidence", batch_evidence)
    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    ctl.accept_validated_plan("plan-1")
    cards = {}

    def snapshot(task_id, *, title="", body=""):
        native = {"id": task_id, "status": "blocked", "assignee": "implementer",
                  "title": title, "body": body, "workspace": f"dir:{tmp_path}",
                  "workspace_path": str(tmp_path)}
        digest = hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest()
        return BoardSnapshot(native_task=native, parents=(), runs=(), comments=(), events=(),
                             attachments=(), observed_at="unit", digest=digest)

    clock = {"value": 0}

    def observed(snapshot):
        clock["value"] += 1
        return BoardSnapshot(native_task=snapshot.native_task, parents=snapshot.parents, runs=snapshot.runs,
            comments=snapshot.comments, events=snapshot.events, attachments=snapshot.attachments,
            observed_at=f"observation-{clock['value']}", digest=snapshot.digest)

    def read_task(task_id):
        if task_id == "anchor-A":
            return BoardSnapshot(native_task={"id": "anchor-A", "status": "ready"}, parents=(), runs=(),
                comments=(), events=(), attachments=(), observed_at="unit", digest="anchor-digest")
        return observed(cards[task_id])

    def create_held(action, *, title, body, **kwargs):
        task_id = "native-" + action.target["ticket_id"]
        cards[task_id] = snapshot(task_id, title=title, body=body)
        card = cards[task_id]
        show_task = dict(card.native_task)
        show_task["workspace_path"] = str(tmp_path)
        show = {"task": show_task, "parents": [], "children": [], "runs": [],
                "events": [], "comments": [], "latest_summary": None}
        readback = card.to_dict()
        readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1", "show": show, "runs": []}
        return ActionResult(action.key, "verified", "created", readback)

    def verify_effect(action):
        task_id = "native-" + action.target["ticket_id"]
        return ActionResult(action.key, "verified", "exact current card", observed(cards[task_id]).to_dict())

    board.read_task = read_task
    board.create_held = create_held
    board.verify_effect = verify_effect
    return ctl, store, board, cards


def _third_card_proposal(req):
    from local_first_orchestrator.decomposition import TranchePlan
    from local_first_orchestrator.decomposition_planner import PlanProposal, TrancheSemantics
    original = _batch_proposal(req)
    tranche = original.plan.tranches[0]
    third = dataclasses.replace(tranche.tickets[0], ticket_id="TK-C", criterion_ids=("AC-1",),
                                dependencies=())
    plan = dataclasses.replace(original.plan,
        tranches=(TranchePlan("TR-A", 0, tranche.tickets + (third,), ("AC-1", "AC-2")),),
        criterion_coverage={"AC-1": ("TK-A", "TK-C"), "AC-2": ("TK-B",)})
    return PlanProposal(req.identity, plan, original.tranche_semantics)


def _serial_three_card_proposal(req):
    """Keep the batch's A -> B edge and add B -> C for v2 sequencing tests."""
    from local_first_orchestrator.decomposition_planner import PlanProposal

    original = _third_card_proposal(req)
    tranche = original.plan.tranches[0]
    ticket_a, ticket_b, ticket_c = tranche.tickets
    ticket_c = dataclasses.replace(ticket_c, dependencies=(ticket_b.ticket_id,))
    plan = dataclasses.replace(original.plan,
        tranches=(dataclasses.replace(tranche, tickets=(ticket_a, ticket_b, ticket_c)),))
    return PlanProposal(req.identity, plan, original.tranche_semantics)


def _three_card_fixture(tmp_path, monkeypatch, *, proposal_factory=_third_card_proposal):
    import tests.test_m4_plan_acceptance as acceptance
    original = acceptance.evidence

    def third_evidence():
        base = original()
        req = acceptance.base_request()
        return evidence_payload(req, proposal_factory(req),
            planner_task_id=base["planner"]["task_id"], planner_run_id=base["planner"]["run_id"],
            planner_session_id=base["planner"]["session_id"], planner_profile=base["planner"]["profile"])

    monkeypatch.setattr(acceptance, "evidence", third_evidence)
    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    ctl.accept_validated_plan("plan-1")
    cards = {}

    def snapshot(task_id, *, title="", body=""):
        native = {"id": task_id, "status": "blocked", "assignee": "implementer",
                  "title": title, "body": body, "workspace": f"dir:{tmp_path}",
                  "workspace_path": str(tmp_path)}
        digest = hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest()
        return BoardSnapshot(native_task=native, parents=(), runs=(), comments=(), events=(),
                             attachments=(), observed_at="unit", digest=digest)

    clock = {"value": 0}
    def observed(item):
        clock["value"] += 1
        return BoardSnapshot(native_task=item.native_task, parents=item.parents, runs=item.runs,
            comments=item.comments, events=item.events, attachments=item.attachments,
            observed_at=f"observation-{clock['value']}", digest=item.digest)

    def read_task(task_id):
        if task_id == "anchor-A":
            return BoardSnapshot(native_task={"id": "anchor-A", "status": "ready"}, parents=(), runs=(),
                comments=(), events=(), attachments=(), observed_at="unit", digest="anchor-digest")
        return observed(cards[task_id])

    def create_held(action, *, title, body, **kwargs):
        task_id = "native-" + action.target["ticket_id"]
        cards[task_id] = snapshot(task_id, title=title, body=body)
        card = cards[task_id]
        show_task = dict(card.native_task)
        show_task["workspace_path"] = str(tmp_path)
        show = {"task": show_task, "parents": [], "children": [], "runs": [],
                "events": [], "comments": [], "latest_summary": None}
        readback = card.to_dict()
        readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1", "show": show, "runs": []}
        return ActionResult(action.key, "verified", "created", readback)

    def verify_effect(action):
        task_id = "native-" + action.target["ticket_id"]
        return ActionResult(action.key, "verified", "exact current card", observed(cards[task_id]).to_dict())

    board.read_task, board.create_held, board.verify_effect = read_task, create_held, verify_effect
    return ctl, store, board, cards


def test_third_accepted_card_is_frozen_and_required_for_first_edge_authority(tmp_path, monkeypatch):
    ctl, store, board, cards = _three_card_fixture(tmp_path, monkeypatch)
    try:
        for ticket in ("TK-A", "TK-B", "TK-C"):
            assert ctl.prepare_active_piece("plan-1", ticket)["outcome"] == "held"
        before = _database_rows(store)
        first = ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        second = ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert first["operation_key"] == second["operation_key"]
        assert first["frozen_create_receipts"] == second["frozen_create_receipts"]
        assert len(first["frozen_create_receipts"]) == 3
        assert {r["ticket_id"] for r in first["frozen_create_receipts"]} == {"TK-A", "TK-B", "TK-C"}
        assert _database_rows(store) == before

        for fault in ("missing", "drift", "running"):
            original = cards.get("native-TK-C")
            if fault == "missing":
                del cards["native-TK-C"]
            elif fault == "drift":
                cards["native-TK-C"] = BoardSnapshot(native_task=original.native_task, parents=original.parents,
                    runs=original.runs, comments=({"body": "changed"},), events=original.events,
                    attachments=original.attachments, observed_at=original.observed_at, digest=original.digest)
            else:
                cards["native-TK-C"] = BoardSnapshot(native_task=original.native_task, parents=original.parents,
                    runs=({"id": "run-C", "status": "running"},), comments=original.comments,
                    events=original.events, attachments=original.attachments,
                    observed_at=original.observed_at, digest=original.digest)
            state = _database_rows(store)
            with pytest.raises((ValueError, KeyError)):
                ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
            assert _database_rows(store) == state
            if fault == "missing":
                cards["native-TK-C"] = original
            else:
                cards["native-TK-C"] = original
    finally:
        store.close()


def _seed_two(ctl):
    assert ctl.prepare_active_piece("plan-1", "TK-A")["outcome"] == "held"
    assert ctl.prepare_active_piece("plan-1", "TK-B")["outcome"] == "held"


def test_valid_firstedge_authority_is_stable_frozen_and_read_only(tmp_path, monkeypatch):
    ctl, store, _board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(ctl)
        before = (_database_rows(store), store.read_scope(SCOPE)["members"])
        first = ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        second = ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert first["kind"] == "accepted_active_tranche_native_link_v1"
        assert first["source_ticket_id"] == "TK-A" and first["target_ticket_id"] == "TK-B"
        assert first["read_only_first_edge_only"] is True
        assert first["operation_key"] == second["operation_key"]
        assert first["frozen_create_receipts"] == second["frozen_create_receipts"]
        assert len(first["frozen_create_receipts"]) == 2
        for receipt in first["frozen_create_receipts"]:
            capture = receipt["readback"]["raw_capture_v1"]
            assert capture["kind"] == "hermes_kanban_raw_capture_v1"
            assert capture["show"]["task"]["id"] == receipt["task_id"]
            assert capture["show"]["children"] == ()
            assert capture["runs"] == ()
        with pytest.raises(TypeError):
            first["plan_id"] = "changed"
        with pytest.raises(TypeError):
            first["frozen_create_receipts"][0]["readback"]["native_task"]["assignee"] = "changed"
        with pytest.raises(AttributeError):
            first["frozen_create_receipts"][0]["readback"]["parents"].append({"id": "bad"})
        assert first["frozen_create_receipts"][0]["readback"]["native_task"]["assignee"] == "implementer"
        assert (_database_rows(store), store.read_scope(SCOPE)["members"]) == before
    finally:
        store.close()


@pytest.mark.parametrize("target,dependency", [
    ("TK-A", "TK-B"), ("TK-A", "TK-A"), ("TK-B", "TK-FOREIGN"),
])
def test_undeclared_reversed_self_and_foreign_edges_reject_before_board_reads(
    tmp_path, monkeypatch, target, dependency
):
    ctl, store, board, _cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(ctl)
        reads = []
        original = board.read_task
        board.read_task = lambda task_id: (reads.append(task_id), original(task_id))[1]
        before = _database_rows(store)
        with pytest.raises(ValueError):
            ctl.active_piece_dependency_authority("plan-1", target, dependency)
        assert reads == []
        assert _database_rows(store) == before
    finally:
        store.close()


def test_fresh_content_difference_rejects_even_when_digest_is_unchanged(tmp_path, monkeypatch):
    ctl, store, _board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(ctl)
        original = cards["native-TK-A"]
        cards["native-TK-A"] = BoardSnapshot(native_task=original.native_task, parents=original.parents,
            runs=original.runs, comments=({"body": "human comment"},), events=original.events,
            attachments=original.attachments, observed_at=original.observed_at, digest=original.digest)
        before = _database_rows(store)
        with pytest.raises(ValueError, match="fresh native card differs"):
            ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("drift", ["status", "parent", "running", "digest"])
def test_any_firstedge_held_receipt_drift_rejects_read_only(tmp_path, monkeypatch, drift):
    ctl, store, _board, cards = _batch_fixture(tmp_path, monkeypatch)
    try:
        _seed_two(ctl)
        task_id = "native-TK-A"
        original = cards[task_id]
        native = dict(original.native_task)
        parents, runs, digest = original.parents, original.runs, original.digest
        if drift == "status": native["status"] = "ready"
        elif drift == "parent": parents = ({"id": "native-other"},)
        elif drift == "running": runs = ({"id": "run-1", "status": "running"},)
        else: digest = "forged-digest"
        cards[task_id] = BoardSnapshot(native_task=native, parents=parents, runs=runs,
            comments=original.comments, events=original.events, attachments=original.attachments,
            observed_at=original.observed_at, digest=digest)
        before = _database_rows(store)
        with pytest.raises(ValueError):
            ctl.active_piece_dependency_authority("plan-1", "TK-B", "TK-A")
        assert _database_rows(store) == before
    finally:
        store.close()
