"""M5 crash/reopen tests for public pause containment and authorized resume.

The only durable state is EvidenceStore's SQLite journal.  The fixture board is
external native state: it survives coordinator/store reconstruction and exposes
an exact per-action marker only after a native mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember, OperationIntent, PauseIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore


SCOPE = {"board_id": "m5-restart-board", "anchor_task_id": "m5-anchor"}


class InjectedCrash(BaseException):
    """Simulate process death: coordinator catches Exception, not BaseException."""


def _snapshot(task_id: str, status: str, revision: int, runs: tuple[Mapping[str, object], ...] = ()) -> BoardSnapshot:
    return BoardSnapshot(
        {"id": task_id, "status": status}, (), runs, (), (), (), "fixture-now",
        f"{task_id}:{status}:revision-{revision}",
    )


@dataclass
class MarkerBoard:
    """Production-shaped native adapter fake with durable-external marker state."""

    cards: dict[str, BoardSnapshot] = field(default_factory=lambda: {
        "m5-anchor": _snapshot("m5-anchor", "blocked", 0),
        "m5-task": _snapshot("m5-task", "ready", 0),
    })
    sends: list[str] = field(default_factory=list)
    verification_reads: list[str] = field(default_factory=list)
    markers: set[str] = field(default_factory=set)

    is_fake = True

    def read_task(self, task_id: str) -> BoardSnapshot:
        return self.cards[task_id]

    def _mutate(self, action: Action, status: str, details: str) -> ActionResult:
        self.sends.append(action.key)
        before = self.read_task(action.target["task_id"])
        revision = int(before.digest.rsplit("-", 1)[1]) + 1
        after = _snapshot(action.target["task_id"], status, revision, before.runs)
        self.cards[action.target["task_id"]] = after
        self.markers.add(action.key)
        return ActionResult(action.key, "verified", details, after.to_dict())

    def hold(self, action: Action, task_id: str, reason: str) -> ActionResult:
        assert task_id == action.target["task_id"] and reason
        return self._mutate(action, "blocked", "native hold applied")

    def release(self, action: Action, task_id: str, reason: str) -> ActionResult:
        assert task_id == action.target["task_id"] and reason
        return self._mutate(action, "ready", "native release applied")

    def stop_run(self, action: Action, task_id: str, run_id: str, reason: str) -> ActionResult:
        assert task_id == action.target["task_id"] and run_id and reason
        return ActionResult(action.key, "unsupported", "fixture has no native stop capability", self.read_task(task_id).to_dict())

    def verify_effect(self, action: Action) -> ActionResult:
        # This is a read-only exact marker verifier; it never calls hold/release.
        self.verification_reads.append(action.key)
        if action.key not in self.markers:
            return ActionResult(action.key, "unsupported", "exact native marker absent", self.read_task(action.target["task_id"]).to_dict())
        return ActionResult(action.key, "verified", "exact native marker verified", self.read_task(action.target["task_id"]).to_dict())


def _open(database: Path, board: MarkerBoard, lock_path: Path) -> tuple[Coordinator, EvidenceStore]:
    store = EvidenceStore.open(database, create_new=not database.exists())
    store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember("m5-restart-board", "m5-anchor", "m5-task", "implementation", 0, (), "m5-member"))
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path), budget_policy=BudgetPolicy(2, 2, 2, 2, 2)), store


def _prepare(tmp_path: Path, family: str) -> tuple[MarkerBoard, Path, Path, Callable[[Coordinator], dict[str, object]]]:
    board = MarkerBoard()
    database, lock_path = tmp_path / "effects.sqlite", tmp_path / "coordinator.lock"
    coordinator, store = _open(database, board, lock_path)
    if family == "hold":
        invoke = lambda current: current.pause()
    else:
        # Establish the hold only through the public API, then test the release
        # effect itself.  Its marker/sends remain observable external state.
        assert coordinator.pause()["outcome"] == "verified"
        invoke = lambda current: current.resume(authorized_clear=True)
    store.close()
    return board, database, lock_path, invoke


def _effect_operation(store: EvidenceStore, family: str):
    operations = [operation for operation in store.read_scope(SCOPE)["operations"] if operation.effect == family]
    assert len(operations) == 1
    return operations[0]


def _effect_sends(board: MarkerBoard, family: str) -> list[str]:
    prefix = f"{family}:"
    return [key for key in board.sends if key.startswith(prefix)]


def _budget_events(store: EvidenceStore) -> tuple[dict[str, object], ...]:
    return tuple(store.read_scope(SCOPE)["budget_events"])


def _inject_before_claim(store: EvidenceStore, _board: MarkerBoard, key: str, _family: str) -> None:
    original = store.begin_effect_attempt

    def crash_before_claim(scope, operation_key):
        if operation_key.startswith(f"{_family}:m5-task:"):
            raise InjectedCrash("before durable attempt claim")
        return original(scope, operation_key)

    store.begin_effect_attempt = crash_before_claim  # type: ignore[method-assign]


def _inject_after_claim_before_send(store: EvidenceStore, _board: MarkerBoard, key: str, _family: str) -> None:
    original = store.begin_effect_attempt

    def crash_after_claim(scope, operation_key):
        claimed = original(scope, operation_key)
        if operation_key.startswith(f"{_family}:m5-task:"):
            raise InjectedCrash("after durable claim before native send")
        return claimed

    store.begin_effect_attempt = crash_after_claim  # type: ignore[method-assign]


def _inject_after_effect_before_ack(store: EvidenceStore, _board: MarkerBoard, key: str, _family: str) -> None:
    original = store.ack_effect

    def crash_before_ack(scope, operation_key, *, readback, outcome="verified"):
        if operation_key.startswith(f"{_family}:m5-task:"):
            raise InjectedCrash("after native effect before durable acknowledgement")
        return original(scope, operation_key, readback=readback, outcome=outcome)

    store.ack_effect = crash_before_ack  # type: ignore[method-assign]


def _inject_after_ack(store: EvidenceStore, _board: MarkerBoard, key: str, _family: str) -> None:
    original = store.ack_effect

    def crash_after_ack(scope, operation_key, *, readback, outcome="verified"):
        acknowledged = original(scope, operation_key, readback=readback, outcome=outcome)
        if operation_key.startswith(f"{_family}:m5-task:"):
            raise InjectedCrash("after durable acknowledgement")
        return acknowledged

    store.ack_effect = crash_after_ack  # type: ignore[method-assign]


def test_resume_clears_two_held_members_with_historical_blocked_run_evidence(tmp_path: Path) -> None:
    """The current EvidenceStore release journal retains inert blocked history."""
    board = MarkerBoard()
    database, lock_path = tmp_path / "effects.sqlite", tmp_path / "coordinator.lock"
    coordinator, store = _open(database, board, lock_path)
    try:
        board.cards["m5-task"] = _snapshot(
            "m5-task", "ready", 0, ({"id": "historical-block", "status": "blocked"},),
        )
        store.register_member(ManagedMember(
            "m5-restart-board", "m5-anchor", "m5-anchor", "planning", 0, (), "anchor-member",
        ))

        assert coordinator.pause()["outcome"] == "verified"
        first_release = coordinator.resume(authorized_clear=True)
        assert first_release["reason"] == "resuming_release_in_progress"
        assert board.read_task("m5-task").runs == ({"id": "historical-block", "status": "blocked"},)
        assert store.read_scope(SCOPE)["operator_intent"] is not None

        second_release = coordinator.resume(authorized_clear=True)
        assert second_release["reason"] == "resuming_release_in_progress"
        assert coordinator.resume(authorized_clear=True) == {
            "outcome": "verified", "reason": "release_verified", "actions_attempted": 0,
        }
        assert _effect_sends(board, "release") == [
            "release:m5-anchor:m5-anchor:blocked:revision-0:resume:1",
            "release:m5-task:m5-task:blocked:revision-1:resume:1",
        ]
        assert store.read_scope(SCOPE)["operator_intent"] is not None
        assert not store.read_scope(SCOPE)["operator_intent"].active
    finally:
        store.close()


def test_repeated_public_pause_resume_generates_fresh_release_keys_for_recurring_held_digests(tmp_path: Path) -> None:
    """Two complete pause cycles must not treat recurring held digests as old acks."""
    board = MarkerBoard()
    database, lock_path = tmp_path / "effects.sqlite", tmp_path / "coordinator.lock"
    coordinator, store = _open(database, board, lock_path)
    cycle_keys = []
    budgets_before_second_cycle = None
    try:
        board.cards["m5-task"] = _snapshot(
            "m5-task", "ready", 0, ({"id": "historical-block", "status": "blocked"},),
        )
        store.register_member(ManagedMember(
            "m5-restart-board", "m5-anchor", "m5-anchor", "planning", 0, (), "anchor-member",
        ))
        for cycle in range(2):
            sends_before_cycle = len(board.sends)
            if cycle:
                # The fixture intentionally repeats the exact native inputs that
                # produced the first held digest; only the durable generation is new.
                board.cards["m5-anchor"] = _snapshot("m5-anchor", "blocked", 0)
                board.cards["m5-task"] = _snapshot(
                    "m5-task", "ready", 0, ({"id": "historical-block", "status": "blocked"},),
                )
            paused = coordinator.pause()
            while paused["outcome"] != "verified":
                assert paused["outcome"] == "partial", (cycle, paused)
                paused = coordinator.tick()
            assert board.read_task("m5-task").runs == ({"id": "historical-block", "status": "blocked"},)
            held = tuple(member.task_id for member in store.read_scope(SCOPE)["members"])
            assert all(board.read_task(task_id).native_task["status"] == "blocked" for task_id in held)

            release_keys = None
            for _ in held:
                assert coordinator.resume(authorized_clear=True) == {
                    "outcome": "partial", "reason": "resuming_release_in_progress", "actions_attempted": 1,
                }
                current_intent = store.read_scope(SCOPE)["operator_intent"]
                assert current_intent is not None and current_intent.active and current_intent.resuming
                if release_keys is None:
                    release_keys = dict(current_intent.resuming_action_keys)
            assert coordinator.resume(authorized_clear=True) == {
                "outcome": "verified", "reason": "release_verified", "actions_attempted": 0,
            }
            state = store.read_scope(SCOPE)
            assert release_keys is not None and set(release_keys) == set(held)
            cycle_keys.append(tuple(release_keys[task_id] for task_id in held))
            assert len(cycle_keys[-1]) == 2
            applied_keys = {op.key for op in state["operations"] if op.effect == "release" and op.phase == "applied"}
            assert set(cycle_keys[-1]).issubset(applied_keys)
            fresh_release_sends = [key for key in board.sends[sends_before_cycle:] if key in set(cycle_keys[-1])]
            assert set(fresh_release_sends) == set(cycle_keys[-1])
            assert all(fresh_release_sends.count(key) == 1 and key in board.markers for key in cycle_keys[-1])
            assert board.read_task("m5-task").runs == ({"id": "historical-block", "status": "blocked"},)
            assert state["operator_intent"] is not None and state["operator_intent"].active is False
            if cycle == 0:
                budgets_before_second_cycle = tuple(state["budget_events"])
                store.close()
                coordinator, store = _open(database, board, lock_path)
    finally:
        store.close()

    assert len(cycle_keys) == 2
    assert set(cycle_keys[0]).isdisjoint(cycle_keys[1])
    assert all(len(keys) == 2 for keys in cycle_keys)
    reopened = EvidenceStore.open(database)
    try:
        state = reopened.read_scope(SCOPE)
        assert tuple(state["budget_events"]) == budgets_before_second_cycle
        applied = {op.key: op for op in state["operations"] if op.effect == "release" and op.phase == "applied"}
        assert set(applied) == set(cycle_keys[0] + cycle_keys[1])
        assert all(op.key in board.markers for op in applied.values())
    finally:
        reopened.close()


def test_resuming_generation_ten_selects_only_generation_nine_hold() -> None:
    """The resuming generation is a release generation, never its hold generation."""
    baseline = "same-recurring-baseline"
    resume = PauseIntent(SCOPE, "operator", 10, False, False, active=True,
                         managed_task_ids=("m5-task",), baseline_digests={"m5-task": baseline},
                         resuming=True, resuming_task_ids=("m5-task",),
                         resuming_action_keys={"m5-task": "release:m5-task"})
    original_hold = OperationIntent("hold:m5-task:same-recurring-baseline:pause:9", SCOPE,
                                    {"task_id": "m5-task"}, "hold", baseline, {}, "verified", None, {}, "applied")
    release_generation_hold = OperationIntent("hold:m5-task:same-recurring-baseline:pause:10", SCOPE,
                                              {"task_id": "m5-task"}, "hold", baseline, {}, "verified", None, {}, "applied")
    assert Coordinator._matches_current_pause_hold(original_hold, resume)
    assert not Coordinator._matches_current_pause_hold(release_generation_hold, resume)


def test_title_adoption_rejects_unbound_historical_hold_receipt(tmp_path: Path) -> None:
    """A matching old hold readback is not cycle-start authority for an already-held task."""
    board = MarkerBoard()
    database, lock_path = tmp_path / "effects.sqlite", tmp_path / "coordinator.lock"
    coordinator, store = _open(database, board, lock_path)
    try:
        before = BoardSnapshot({"id": "m5-task", "status": "blocked", "title": "original"}, (), (), (), (), (),
                               "fixture-now", "held-baseline")
        board.cards["m5-task"] = before
        intent = PauseIntent(SCOPE, "operator", 9, False, False, active=True,
                             managed_task_ids=("m5-task",), baseline_digests={"m5-task": before.digest})
        historical = OperationIntent("hold:m5-task:held-baseline:pause:8", SCOPE, {"task_id": "m5-task"}, "hold",
                                     before.digest, {}, "verified", before.to_dict(), {}, "applied")
        accepted = OperationIntent("accept-for-test", SCOPE, {"plan_id": "plan-1"}, "accept_validated_plan",
                                   "accepted", {}, "verified", {"token": {}}, {}, "applied")
        changed = BoardSnapshot({"id": "m5-task", "status": "blocked", "title": "renamed"}, (), (), (), (), (),
                                "fixture-now", "held-after-title-edit")
        coordinator.store.read_accepted_plan = lambda _scope, _plan_id: {"plan_id": "plan-1"}  # type: ignore[method-assign]
        state = {**store.read_scope(SCOPE), "operator_intent": intent, "operations": (accepted, historical)}
        assert coordinator._adopt_paused_metadata_observation_locked(state, (changed,), intent) is None
    finally:
        store.close()


def test_title_adoption_uses_public_cycle_start_evidence_for_already_held_member(tmp_path: Path) -> None:
    """An already-held member remains title-adoptable only through its public pause journal."""
    board = MarkerBoard()
    database, lock_path = tmp_path / "effects.sqlite", tmp_path / "coordinator.lock"
    coordinator, store = _open(database, board, lock_path)
    try:
        before = BoardSnapshot({"id": "m5-task", "status": "blocked", "title": "original"}, (), (), (), (), (),
                               "fixture-now", "public-held-baseline")
        board.cards["m5-task"] = before
        assert coordinator.pause()["outcome"] == "verified"
        intent = store.read_scope(SCOPE)["operator_intent"]
        assert intent is not None and intent.generation == 0
        changed = BoardSnapshot({"id": "m5-task", "status": "blocked", "title": "renamed"}, (), (), (), (), (),
                                "fixture-now", "public-held-after-title-edit")
        accepted = OperationIntent("accept-for-test", SCOPE, {"plan_id": "plan-1"}, "accept_validated_plan",
                                   "accepted", {}, "verified", {"token": {}}, {}, "applied")
        coordinator.store.read_accepted_plan = lambda _scope, _plan_id: {"plan_id": "plan-1"}  # type: ignore[method-assign]
        state = {**store.read_scope(SCOPE), "operations": (accepted,)}
        with coordinator.lock:
            adopted = coordinator._adopt_paused_metadata_observation_locked(state, (changed,), intent)
        assert adopted is not None and adopted["outcome"] == "adopted"
    finally:
        store.close()


INJECTIONS = {
    "before_claim": _inject_before_claim,
    "after_claim_before_send": _inject_after_claim_before_send,
    "after_effect_before_ack": _inject_after_effect_before_ack,
    "after_ack": _inject_after_ack,
}


@pytest.mark.parametrize("family", ("hold", "release"))
@pytest.mark.parametrize("boundary", tuple(INJECTIONS))
def test_m5_hold_release_crash_reopen_boundaries(tmp_path: Path, family: str, boundary: str) -> None:
    """Crash at the actual journal/native boundary, close DB, then use a fresh coordinator."""
    board, database, lock_path, invoke = _prepare(tmp_path, family)
    crashing, store = _open(database, board, lock_path)
    try:
        # The action key is generated only by the public planner.  Fault injection
        # selects its exact family/task key at the store acknowledgement boundary.
        INJECTIONS[boundary](store, board, "planner-generated", family)
        with pytest.raises(InjectedCrash):
            invoke(crashing)
        operation = _effect_operation(store, family)
        action_key = operation.key
        sends_at_crash = tuple(_effect_sends(board, family))
        budgets_at_crash = _budget_events(store)
        if boundary == "before_claim":
            assert operation.phase == "pending"
            assert sends_at_crash == ()
        elif boundary == "after_claim_before_send":
            assert operation.phase == "unknown"
            assert sends_at_crash == ()
            assert action_key not in board.markers
        elif boundary == "after_effect_before_ack":
            assert operation.phase == "unknown"
            assert sends_at_crash == (action_key,)
            assert action_key in board.markers
        else:
            assert operation.phase == "applied"
            assert sends_at_crash == (action_key,)
            assert action_key in board.markers
    finally:
        store.close()

    # A real close/open creates a new SQLite connection and a fresh coordinator;
    # only MarkerBoard's native state survives the simulated process death.
    resumed, reopened = _open(database, board, lock_path)
    try:
        if boundary == "before_claim":
            # A claim never committed, so a public poll is allowed to make the
            # one first native send after reopen (not a blind unknown replay).
            result = resumed.tick() if family == "hold" else resumed.resume(authorized_clear=True)
            assert result["actions_attempted"] == 1
            assert _effect_sends(board, family) == [action_key]
            assert _effect_operation(reopened, family).phase == "applied"
        elif boundary == "after_claim_before_send":
            # The claim is conservative unknown even though the adapter was never
            # reached: reconciliation is read-only and cannot send or ack it.
            result = resumed.reconcile()
            assert result["outcome"] == "partial"
            assert _effect_sends(board, family) == []
            assert action_key in board.verification_reads
            assert _effect_operation(reopened, family).phase == "unknown"
            assert reopened.pending_operations(SCOPE)[0].key == action_key
        elif boundary == "after_effect_before_ack":
            # Native state has the exact marker, so only a fresh read can ack.
            result = resumed.reconcile()
            assert result["outcome"] == "verified"
            assert _effect_sends(board, family) == [action_key]
            assert board.verification_reads[-1] == action_key
            assert _effect_operation(reopened, family).phase == "applied"
        else:
            # Durable acknowledgement replays with zero native sends; execute the
            # public lifecycle entrypoint rather than inspecting source behavior.
            result = resumed.tick() if family == "hold" else resumed.resume(authorized_clear=True)
            assert result["actions_attempted"] == 0
            assert _effect_sends(board, family) == [action_key]
            assert _effect_operation(reopened, family).phase == "applied"
        # Pause/release consume no repair admission; every restart branch must
        # preserve the exact immutable budget ledger (never duplicate a charge).
        assert _budget_events(reopened) == budgets_at_crash
    finally:
        reopened.close()
