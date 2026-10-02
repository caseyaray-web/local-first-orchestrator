from __future__ import annotations

import dataclasses

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter

SCOPE = {"board_id": "board-A", "anchor_task_id": "anchor-A"}


def snapshot(*, status="blocked", runs=(), digest="anchor-digest", marker=None):
    comments = () if marker is None else ({"body": f"BLOCKED: {marker}"},)
    events = () if marker is None else ({"kind": "blocked", "payload": {
        "reason": marker, "kind": "needs_input", "source_status": "ready", "recurrences": 1}},)
    return BoardSnapshot({"id": "anchor-A", "status": status}, (), tuple(runs), comments, events, (), "now", digest)


class Board:
    is_fake = True

    def __init__(self, current=None):
        self.current = current

    def read_task(self, task_id):
        assert task_id == "anchor-A"
        return self.current

    def hold(self, *args, **kwargs):
        raise AssertionError("prerequisite validation must not hold or mutate")

    def release(self, *args, **kwargs):
        raise AssertionError("prerequisite validation must not release or mutate")

    def stop_run(self, *args, **kwargs):
        raise AssertionError("prerequisite validation must not stop or mutate")

    _native_marker = staticmethod(HermesBoardAdapter._native_marker)
    _contains_marker = staticmethod(HermesBoardAdapter._contains_marker)
    _running = staticmethod(HermesBoardAdapter._running)
    _verify_native_hold_release = HermesBoardAdapter._verify_native_hold_release


def controller(tmp_path, current):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True)
    store.migrate()
    return Coordinator(SCOPE, board=Board(current), store=store,
        lock=instance_lock(tmp_path / "lock"), budget_policy=BudgetPolicy(1, 1, 1, 1, 1),
        configured_roles={"implementation_profile": "implementer", "local_review_profile": "reviewer", "planning_profile": "planner"}), store


def enroll_and_hold(store, proof, *, key="root-hold"):
    store.register_member(ManagedMember("board-A", "anchor-A", "anchor-A", "root", 0, (), "native-root"))
    intent = OperationIntent(key, SCOPE, {"task_id": "anchor-A"}, "hold", proof.digest,
        {"task_id": "anchor-A", "before_digest": "before"}, None, None, {"reconcile_only": True}, "pending")
    store.reserve_operation(intent)
    store.ack_effect(SCOPE, intent.key, readback=proof.to_dict(), outcome="verified")


@pytest.mark.parametrize("case", ["missing", "ambiguous", "wrong-root", "ready", "running", "stale-proof"])
def test_anchor_prerequisite_rejects_before_managed_reservation_or_native_effect(tmp_path, case):
    current = snapshot(status="ready" if case == "ready" else "blocked",
        runs=({"id": "run", "status": "running"},) if case == "running" else (),
        digest="fresh" if case == "stale-proof" else "anchor-digest")
    ctl, store = controller(tmp_path, current)
    try:
        if case not in {"missing", "wrong-root"}:
            enroll_and_hold(store, snapshot(digest="old" if case == "stale-proof" else "anchor-digest"))
        if case == "ambiguous":
            store.register_member(ManagedMember("board-A", "anchor-A", "other", "root", 1, (), "other-root"))
        if case == "wrong-root":
            store.register_member(ManagedMember("board-A", "anchor-A", "other", "root", 0, (), "native-root"))
        with pytest.raises(ValueError, match="enrolled and held"):
            ctl._require_enrolled_held_anchor(store.read_scope(SCOPE))
        assert not [op for op in store.read_scope(SCOPE)["operations"] if op.effect in {"create_held", "release"}]
    finally:
        store.close()


def test_anchor_prerequisite_accepts_exact_enrolled_fresh_native_hold_and_replay(tmp_path):
    ctl, store = controller(tmp_path, snapshot(marker=marker()))
    try:
        enroll_and_hold(store, snapshot(marker=marker()))
        assert ctl._require_enrolled_held_anchor(store.read_scope(SCOPE)).digest == "anchor-digest"
        assert ctl._require_enrolled_held_anchor(store.read_scope(SCOPE)).digest == "anchor-digest"
    finally:
        store.close()


def marker(key="root-hold"):
    from local_first_orchestrator.contracts import Action
    return Board()._native_marker(Action(key, SCOPE, {"task_id": "anchor-A"}, "hold", "anchor-digest"))


def test_anchor_prerequisite_uses_current_matching_native_authority_not_stale_history(tmp_path):
    ctl, store = controller(tmp_path, snapshot(marker=marker("current")))
    try:
        enroll_and_hold(store, snapshot(digest="old", marker=marker("old")), key="old")
        enroll_and_hold(store, snapshot(marker=marker("current")), key="current")
        assert ctl._require_enrolled_held_anchor(store.read_scope(SCOPE)).digest == "anchor-digest"
    finally:
        store.close()


@pytest.mark.parametrize("case", ["none-current", "duplicate-current", "active", "content-drift"])
def test_anchor_prerequisite_refuses_nonunique_or_nonterminal_current_native_hold(tmp_path, case):
    current = snapshot(marker=marker("current"), runs=({"status": "running"},) if case == "active" else ())
    if case == "content-drift":
        current = dataclasses.replace(current, native_task={"id": "anchor-A", "status": "blocked", "body": "drift"})
    if case == "duplicate-current":
        current = dataclasses.replace(current,
            comments=({"body": f"BLOCKED: {marker('current')}"}, {"body": f"BLOCKED: {marker('duplicate')}"}),
            events=({"kind": "blocked", "payload": {"reason": marker("current"), "kind": "needs_input", "source_status": "ready", "recurrences": 1}},
                    {"kind": "blocked", "payload": {"reason": marker("duplicate"), "kind": "needs_input", "source_status": "ready", "recurrences": 1}}))
    ctl, store = controller(tmp_path, current)
    try:
        enroll_and_hold(store, snapshot(digest="old", marker=marker("old")), key="old")
        if case == "duplicate-current":
            enroll_and_hold(store, current, key="current")
            enroll_and_hold(store, current, key="duplicate")
        elif case != "none-current":
            enroll_and_hold(store, snapshot(marker=marker("current")), key="current")
        with pytest.raises(ValueError, match="held anchor"):
            ctl._require_enrolled_held_anchor(store.read_scope(SCOPE))
    finally:
        store.close()
