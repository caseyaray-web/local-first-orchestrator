from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_first_orchestrator.decomposition_planner import serialize_proposal
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_m4_plan_evidence import proposal
from tests.test_m4_planner_creation import native_fixture as _native_fixture, setup
from tests.test_m4_planner_run_binding import _claim
from tests.test_m4_plan_acceptance import _pure_acceptance_fixture, _authority_rows


@pytest.fixture
def native_fixture(tmp_path):
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("set HERMES_M0_CLI to pinned Hermes CLI")
    return _native_fixture.__wrapped__(tmp_path)


def _accepted_plan(tmp_path, native_fixture, monkeypatch):
    board, anchor, workspace, adapter, membership, cli = native_fixture
    controller, store, scope, req = setup(tmp_path, native_fixture)
    request_id = "acceptance-request"
    planner = controller.prepare_planner(request_id=request_id)
    controller.release_planner(request_id=request_id)
    run_id = _claim(board, planner["task_id"], tmp_path / "home")
    monkeypatch.setenv("HERMES_KANBAN_TASK", planner["task_id"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    monkeypatch.setenv("HERMES_SESSION_ID", "controlled-active-piece-session")
    controller.register_planning_request(planner["task_id"], request_id=request_id)
    controller.submit_plan(serialize_proposal(proposal(req)), request_id=request_id)
    accepted = controller.accept_validated_plan(proposal(req).plan.plan_id, request_id=request_id)
    assert len([event for event in store.read_scope(scope)["budget_events"]
                if event["event_id"].startswith("paid_capacity:")]) == 1
    return board, anchor, workspace, adapter, membership, cli, controller, store, scope, accepted, request_id


def _piece_task_ids(cli):
    import json
    return {item["id"] for item in json.loads(cli("list", "--json").stdout)}


def test_active_piece_root_gate_counts_all_root_role_members_before_ticket_filter(tmp_path):
    from local_first_orchestrator.contracts import ManagedMember
    ctl, store, _board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        ctl.accept_validated_plan("plan-1")
        extra = ManagedMember("board-A", "anchor-A", "other-root-task", "root", 0, (), "opaque-root-association")
        original_read_scope = store.read_scope
        def hostile_scope(scope):
            state = original_read_scope(scope)
            state["members"] = (*state["members"], extra)
            return state
        store.read_scope = hostile_scope
        before = _authority_rows(store)
        with pytest.raises(ValueError, match="root"):
            ctl.prepare_active_piece("plan-1", "TK-A")
        assert _authority_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_pure_active_piece_post_ack_pause_or_cancel_fences_membership_and_retry(
    tmp_path, monkeypatch, cancel
):
    from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, PauseIntent
    from tests.test_m4_plan_evidence import SCOPE

    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    calls = []
    try:
        ctl.accept_validated_plan("plan-1")

        def read_task(task_id):
            if task_id == "anchor-A":
                return BoardSnapshot(native_task={"id":"anchor-A","status":"ready"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="anchor-digest")
            return BoardSnapshot(native_task={"id":"pure-piece","status":"blocked","assignee":"implementer"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="piece-digest")

        board.read_task = read_task

        def create_held(action, **kwargs):
            calls.append((action, kwargs))
            pending = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == action.key)
            assert pending.effect == "create_held" and pending.phase == "pending"
            receipt = BoardSnapshot(native_task={"id":"pure-piece","status":"blocked","assignee":"implementer"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="piece-digest")
            return ActionResult(action.key, "verified", "pure verified held piece", receipt.to_dict())

        board.create_held = create_held
        original_ack = store.ack_effect

        def ack_then_pause(*args, **kwargs):
            result = original_ack(*args, **kwargs)
            store.set_operator_intent(PauseIntent(SCOPE, "operator", 1, True, cancel))
            return result

        monkeypatch.setattr(store, "ack_effect", ack_then_pause)
        before_budget = tuple(store.read_scope(SCOPE)["budget_events"])
        result = ctl.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "partial", result
        assert any(word in result["reason"] for word in ("pause", "cancellation", "fenced"))
        assert result["actions_attempted"] == 1
        assert len(calls) == 1
        state = store.read_scope(SCOPE)
        operation = next(op for op in state["operations"] if op.key == result["operation_key"])
        assert operation.phase == "applied"
        assert operation.target["task_id"] == "anchor-A"
        assert not [member for member in state["members"] if member.role == "implementation"]
        assert tuple(state["budget_events"]) == before_budget
        with pytest.raises(ValueError, match="pause/cancellation"):
            ctl.prepare_active_piece("plan-1", "TK-A")
        assert len(calls) == 1
        assert not [member for member in store.read_scope(SCOPE)["members"] if member.role == "implementation"]
        assert tuple(store.read_scope(SCOPE)["budget_events"]) == before_budget
    finally:
        store.close()


@pytest.mark.parametrize("generation", [0, 1])
def test_active_piece_rejects_existing_association_for_wrong_task_before_create(
    tmp_path, monkeypatch, generation
):
    from local_first_orchestrator.contracts import ManagedMember
    from local_first_orchestrator.planning_coordinator import (
        ActiveTrancheRoute, first_active_tranche_materialization,
    )

    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    creates = []
    try:
        ctl.accept_validated_plan("plan-1")
        evidence = store.read_plan(ctl.scope, "plan-1")
        target = first_active_tranche_materialization(
            evidence, ActiveTrancheRoute("implementer", str(tmp_path))
        ).targets[0]
        store.register_member(ManagedMember(
            "board-A", "anchor-A", "wrong-existing-task", "implementation",
            generation, (), target.association,
        ))
        before = (
            _authority_rows(store),
            tuple(store.read_scope(ctl.scope)["members"]),
            tuple(store.read_scope(ctl.scope)["operations"]),
            tuple(store.read_scope(ctl.scope)["budget_events"]),
        )

        def forbidden_create(*args, **kwargs):
            creates.append((args, kwargs))
            raise AssertionError("nativecreatecalled")

        monkeypatch.setattr(board, "create_held", forbidden_create, raising=False)
        with pytest.raises(ValueError, match="existing piece association has no applied creation authority"):
            ctl.prepare_active_piece("plan-1", "TK-A")

        after_state = store.read_scope(ctl.scope)
        after = (
            _authority_rows(store), tuple(after_state["members"]),
            tuple(after_state["operations"]), tuple(after_state["budget_events"]),
        )
        assert after == before
        assert creates == []
    finally:
        store.close()


def test_active_piece_rejects_forged_exact_public_store_target_before_io(tmp_path, monkeypatch):
    from local_first_orchestrator.contracts import Action
    from local_first_orchestrator.planning_coordinator import (
        ActiveTrancheRoute, accepted_active_tranche_create_payload,
        first_active_tranche_materialization,
    )

    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    try:
        ctl.accept_validated_plan("plan-1")
        token = store.read_accepted_plan(ctl.scope, "plan-1")
        evidence = store.read_plan(ctl.scope, "plan-1")
        material = first_active_tranche_materialization(
            evidence, ActiveTrancheRoute("implementer", str(ctl.planning_workspace))
        )
        target = material.targets[0]

        def thaw(value):
            from collections.abc import Mapping
            if isinstance(value, Mapping):
                return {key: thaw(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)):
                return [thaw(child) for child in value]
            return value

        canonical = accepted_active_tranche_create_payload(thaw(token), evidence, target)
        forged_target = {
            **dict(canonical.target), "task_id": ctl.scope["anchor_task_id"],
            "anchor_task_id": ctl.scope["anchor_task_id"], "native_parent": False,
            "native_deps": [], "plan_id": "plan-1", "ticket_id": "TK-A",
            "accepted_token_key": canonical.target["accepted_token_key"],
            "assignee": "forged-profile",
        }
        forged = ctl._intent_for(Action(
            target.operation_key, ctl.scope, forged_target, "create_held", "anchor-digest"
        ))
        store.reserve_operation(forged)

        before_authority = _authority_rows(store)
        before_state = store.read_scope(ctl.scope)
        before_members = tuple(before_state["members"])
        before_budget = tuple(before_state["budget_events"])
        before_ops = tuple(before_state["operations"])
        calls = []

        def forbidden(label):
            def fail(*args, **kwargs):
                calls.append(label)
                raise AssertionError(f"native {label} must not be reached")
            return fail

        monkeypatch.setattr(board, "read_task", forbidden("read_task"))
        monkeypatch.setattr(board, "create_held", forbidden("create_held"), raising=False)
        monkeypatch.setattr(board, "verify_effect", forbidden("verify_effect"), raising=False)
        with pytest.raises(ValueError, match="persisted active-piece intent differs from canonical accepted target"):
            ctl.prepare_active_piece("plan-1", "TK-A")

        after_state = store.read_scope(ctl.scope)
        assert _authority_rows(store) == before_authority
        assert tuple(after_state["members"]) == before_members
        assert tuple(after_state["budget_events"]) == before_budget
        assert tuple(after_state["operations"]) == before_ops
        assert calls == []
        forged_after = next(op for op in after_state["operations"] if op.key == target.operation_key)
        assert forged_after.phase == "pending"
        assert forged_after.readback is None
        assert forged_after.outcome is None
    finally:
        store.close()


@pytest.mark.parametrize("verification", ["forged-conflict", "different-verified-digest"])
def test_pure_active_piece_rejects_forged_acknowledged_receipt_read_only(
    tmp_path, monkeypatch, verification
):
    from local_first_orchestrator.contracts import ActionResult, BoardSnapshot
    from local_first_orchestrator.planning_coordinator import (
        ActiveTrancheRoute, accepted_active_tranche_create_payload,
        first_active_tranche_materialization,
    )
    from tests.test_m4_plan_evidence import SCOPE

    ctl, store, board, _observed = _pure_acceptance_fixture(tmp_path)
    creates = []
    verifier_calls = []
    try:
        ctl.accept_validated_plan("plan-1")
        token = store.read_accepted_plan(SCOPE, "plan-1")
        evidence = store.read_plan(SCOPE, "plan-1")
        material = first_active_tranche_materialization(
            evidence, ActiveTrancheRoute("implementer", str(ctl.planning_workspace))
        )
        target = material.targets[0]

        def thaw(value):
            from collections.abc import Mapping
            if isinstance(value, Mapping):
                return {key: thaw(child) for key, child in value.items()}
            if isinstance(value, (tuple, list)):
                return [thaw(child) for child in value]
            return value

        canonical = accepted_active_tranche_create_payload(thaw(token), evidence, target)
        full_target = {**dict(canonical.target), "task_id": SCOPE["anchor_task_id"],
            "anchor_task_id": SCOPE["anchor_task_id"], "native_parent": False, "native_deps": [],
            "plan_id": "plan-1", "ticket_id": "TK-A",
            "accepted_token_key": canonical.target["accepted_token_key"]}
        forged_target = dict(full_target)
        from local_first_orchestrator.contracts import Action
        forged = ctl._intent_for(Action(
            target.operation_key, SCOPE, forged_target, "create_held", "anchor-digest"
        ))
        store.reserve_operation(forged)
        store.ack_effect(SCOPE, target.operation_key,
            readback={"native_task":{"id":"wrong-forged-piece","status":"blocked","assignee":"implementer"},
                      "parents":[],"runs":[],"comments":[],"events":[],"attachments":[],
                      "observed_at":"now","digest":"forged-card"}, outcome="verified")
        before = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == target.operation_key)
        before_budget = tuple(store.read_scope(SCOPE)["budget_events"])
        anchor = BoardSnapshot(native_task={"id":"anchor-A","status":"ready"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="anchor-digest")
        board.read_task = lambda task_id: anchor

        def forbidden_create(*args, **kwargs):
            creates.append((args, kwargs))
            raise AssertionError("native create must not be reached")

        def verify(action):
            verifier_calls.append(action)
            if verification == "forged-conflict":
                return ActionResult(action.key, "conflict", "forged acknowledged card lacks exact canonical marker", None)
            receipt = BoardSnapshot(native_task={"id":"different-piece","status":"blocked","assignee":"implementer"}, parents=(), runs=(), comments=(), events=(), attachments=(), observed_at="now", digest="different-digest")
            return ActionResult(action.key, "verified", "verified different card", receipt.to_dict())

        board.create_held = forbidden_create
        board.verify_effect = verify
        result = ctl.prepare_active_piece("plan-1", "TK-A")
        assert result["outcome"] == "partial", result
        assert result["actions_attempted"] == 0
        assert creates == [] and len(verifier_calls) == 1
        state = store.read_scope(SCOPE)
        assert not [member for member in state["members"] if member.role == "implementation"]
        assert tuple(state["budget_events"]) == before_budget
        after = next(op for op in state["operations"] if op.key == target.operation_key)
        assert after.phase == "applied"
        assert after.readback == before.readback
    finally:
        store.close()


def test_native_active_piece_create_replay_and_later_tranche_rejection(tmp_path, native_fixture, monkeypatch):
    (board, anchor, workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch)
    plan_id = accepted["plan_id"]
    try:
        before_tasks = _piece_task_ids(cli)
        result = controller.prepare_active_piece(plan_id, "TK-A", request_id=request_id)
        assert result["outcome"] == "held", result
        task_id = result["task_id"]
        assert result["actions_attempted"] == 1
        created = adapter.read_task(task_id)
        assert created.native_task["status"] == "blocked"
        assert created.native_task["assignee"] == "implementer"
        assert not created.parents and not created.runs
        assert created.native_task.get("parent_id") in (None, "")
        assert task_id not in before_tasks
        state = store.read_scope(scope)
        member = next(m for m in state["members"] if m.task_id == task_id)
        operation = next(op for op in state["operations"] if op.key == result["operation_key"])
        assert member.role == "implementation" and member.work_association == operation.target["association"]
        assert member.generation == 0
        assert operation.phase == "applied"
        assert operation.target["plan_id"] == plan_id and operation.target["ticket_id"] == "TK-A"
        assert operation.target["native_parent"] is False
        events = [event for event in state["budget_events"] if event["event_id"].startswith("paid_capacity:")]
        assert len(events) == 1
        before_piece_ids = _piece_task_ids(cli)
        before_events = tuple(state["budget_events"])
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        membership["store"] = store
        controller.store = store
        replay = controller.prepare_active_piece(plan_id, "TK-A", request_id=request_id)
        assert replay["outcome"] == "held" and replay["task_id"] == task_id
        assert replay["actions_attempted"] == 0
        assert _piece_task_ids(cli) == before_piece_ids
        state = store.read_scope(scope)
        assert tuple(state["budget_events"]) == before_events
        assert len([m for m in state["members"] if m.work_association == member.work_association]) == 1
        before_reject = (tuple(state["budget_events"]), tuple(state["operations"]), tuple(state["members"]), _piece_task_ids(cli))
        with pytest.raises(ValueError, match="ticket must uniquely belong to accepted tranche zero"):
            controller.prepare_active_piece(plan_id, "TK-B", request_id=request_id)
        state = store.read_scope(scope)
        after_reject = (tuple(state["budget_events"]), tuple(state["operations"]), tuple(state["members"]), _piece_task_ids(cli))
        assert after_reject == before_reject
    finally:
        try:
            store.close()
        except Exception:
            pass


@pytest.mark.parametrize("fault", ["lost_create_response", "post_ack_member_failure"])
def test_native_active_piece_failure_recovery_is_read_only_and_digest_bound(
    tmp_path, native_fixture, monkeypatch, fault
):
    (board, anchor, workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch)
    plan_id = accepted["plan_id"]
    baseline = tuple(store.read_scope(scope)["budget_events"])
    creates = []
    original_invoke = adapter._invoke
    original_register = store.register_member

    def invoke(*args, **kwargs):
        if args and args[0] == "create":
            creates.append(args)
            result = original_invoke(*args, **kwargs)
            if fault == "lost_create_response":
                raise ValueError("injected lost response after native create")
            return result
        return original_invoke(*args, **kwargs)

    def register(member):
        if fault == "post_ack_member_failure" and member.role == "implementation":
            raise RuntimeError("injected membership-store interruption")
        return original_register(member)

    monkeypatch.setattr(adapter, "_invoke", invoke)
    monkeypatch.setattr(store, "register_member", register)
    before_ids = _piece_task_ids(cli)
    first = controller.prepare_active_piece(plan_id, "TK-A", request_id=request_id)
    assert first["outcome"] == "partial", first
    assert first["actions_attempted"] == (1 if fault == "lost_create_response" else 1)
    state = store.read_scope(scope)
    operation = next(op for op in state["operations"] if op.key == first["operation_key"])
    assert operation.phase == ("unknown" if fault == "lost_create_response" else "applied")
    assert not [m for m in state["members"] if m.role == "implementation"]
    assert tuple(state["budget_events"]) == baseline
    assert len(creates) == 1
    if fault == "post_ack_member_failure":
        piece_ids = _piece_task_ids(cli) - before_ids
        assert len(piece_ids) == 1
        task_id = next(iter(piece_ids))
        # The native human edit changes the digest after the immutable receipt.
        cli("comment", task_id, "operator changed card after acknowledgement")
        monkeypatch.setattr(store, "register_member", original_register)
    store.close()
    store = EvidenceStore.open(tmp_path / "evidence.sqlite")
    membership["store"] = store
    controller.store = store
    retry = controller.prepare_active_piece(plan_id, "TK-A", request_id=request_id)
    if fault == "lost_create_response":
        assert retry["outcome"] == "held", retry
        assert retry["actions_attempted"] == 0
        assert retry["task_id"] in _piece_task_ids(cli) - before_ids
        state = store.read_scope(scope)
        recovered = next(op for op in state["operations"] if op.key == first["operation_key"])
        assert recovered.phase == "applied"
        member = next(m for m in state["members"] if m.work_association == recovered.target["association"])
        assert member.generation == 0 and member.task_id == retry["task_id"]
    else:
        assert retry["outcome"] == "partial", retry
        assert "readback" in retry["reason"] or "fresh held-card" in retry["reason"]
        assert not [m for m in store.read_scope(scope)["members"] if m.role == "implementation"]
    assert len(creates) == 1
    assert tuple(store.read_scope(scope)["budget_events"]) == baseline
    store.close()
