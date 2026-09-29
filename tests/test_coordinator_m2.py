from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import pytest

from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}


def snap(task_id: str, state: str, runs=(), *, digest: str | None = None) -> BoardSnapshot:
    native_status = {"held": "blocked"}.get(state, state)
    return BoardSnapshot({"id": task_id, "status": native_status}, (), tuple(runs), (), (), (), "now", digest or f"{task_id}:{native_status}:{len(runs)}")


@dataclass
class FixtureBoard:
    is_fake = True
    snapshots: dict[str, BoardSnapshot]
    calls: list[str] = field(default_factory=list)
    unknown_keys: set[str] = field(default_factory=set)
    unknown_after_effect_keys: set[str] = field(default_factory=set)
    markers: set[str] = field(default_factory=set)

    def read_task(self, task_id: str) -> BoardSnapshot:
        return self.snapshots[task_id]

    def hold(self, action: Action, task_id: str, reason: str) -> ActionResult:
        self.calls.append(action.key)
        if action.key in self.unknown_keys:
            return ActionResult(action.key, "unknown", "lost reply", None)
        before = self.read_task(task_id)
        after = snap(task_id, "blocked", before.runs, digest=before.digest + ":blocked")
        self.snapshots[task_id] = after
        self.markers.add(action.key)
        if action.key in self.unknown_after_effect_keys:
            return ActionResult(action.key, "unknown", "lost reply after native effect", None)
        return ActionResult(action.key, "verified", "held", after.to_dict())

    def release(self, action: Action, task_id: str, reason: str) -> ActionResult:
        self.calls.append(action.key)
        before = self.read_task(task_id)
        after = snap(task_id, "ready", before.runs, digest=before.digest + ":ready")
        self.snapshots[task_id] = after
        self.markers.add(action.key)
        return ActionResult(action.key, "verified", "released", after.to_dict())

    def verify_effect(self, action: Action) -> ActionResult:
        self.calls.append(f"verify:{action.key}")
        if action.key not in self.markers:
            return ActionResult(action.key, "unsupported", "exact native marker absent", self.read_task(action.target["task_id"]).to_dict())
        return ActionResult(action.key, "verified", "exact native marker verified", self.read_task(action.target["task_id"]).to_dict())

    def stop_run(self, action: Action, task_id: str, run_id: str, reason: str) -> ActionResult:
        self.calls.append(action.key)
        return ActionResult(action.key, "unsupported", "fixture native stop unavailable", self.read_task(task_id).to_dict())


def setup(tmp_path, *, state="ready", runs=()):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    board = FixtureBoard({"task": snap("task", state, runs), "anchor": snap("anchor", "held")})
    store.register_member(ManagedMember("fixture-board", "anchor", "task", "implementation", 0, (), "member"))
    return store, board, tmp_path / "coordinator.lock"


def test_status_pause_restart_and_partial_stop_are_durable(tmp_path):
    store, board, lock_path = setup(tmp_path, state="running", runs=({"id": "run-1", "status": "running"},))
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    assert coordinator.status()["native_tasks"]["task"].native_task["status"] == "running"
    result = coordinator.pause(stop=True)
    assert result["outcome"] == "partial"
    assert result["active_workers"] == ("run-1",)
    store.close()
    with EvidenceStore.open(tmp_path / "evidence.sqlite") as restarted:
        restarted_coordinator = Coordinator(SCOPE, board=board, store=restarted, lock=instance_lock(lock_path))
        assert restarted_coordinator.status()["operator_intent"].stop_requested is True


def test_unknown_effect_is_never_resent_and_reconcile_is_read_only(tmp_path):
    store, board, lock_path = setup(tmp_path)
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    board.unknown_keys.add("hold:task:task:ready:0")
    assert coordinator.pause()["outcome"] == "partial"
    assert len(board.calls) == 1
    assert coordinator.reconcile()["outcome"] == "partial"
    assert board.calls == ["hold:task:task:ready:0", "verify:hold:task:task:ready:0"]
    assert store.pending_operations(SCOPE)[0].phase == "unknown"


def test_restart_reconciles_unknown_after_effect_with_exact_marker_without_resend(tmp_path):
    store, board, lock_path = setup(tmp_path)
    key = "hold:task:task:ready:0"
    board.unknown_after_effect_keys.add(key)
    assert Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path)).pause()["outcome"] == "partial"
    assert board.calls == [key]
    store.close()
    with EvidenceStore.open(tmp_path / "evidence.sqlite") as restarted:
        result = Coordinator(SCOPE, board=board, store=restarted, lock=instance_lock(lock_path)).reconcile()
        assert result["outcome"] == "verified"
        assert board.calls == [key, f"verify:{key}"]
        assert restarted.pending_operations(SCOPE) == ()
        assert restarted.read_scope(SCOPE)["effect_observations"][-1]["outcome"] == "verified"


def test_restart_human_edit_against_durable_pause_baseline_fails_closed(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    assert Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path)).pause()["outcome"] == "verified"
    store.close()
    board.snapshots["task"] = snap("task", "held", digest="human-edit-after-restart")
    with EvidenceStore.open(tmp_path / "evidence.sqlite") as restarted:
        result = Coordinator(SCOPE, board=board, store=restarted, lock=instance_lock(lock_path)).reconcile()
        assert result["outcome"] == "conflict"
        assert Coordinator(SCOPE, board=board, store=restarted, lock=instance_lock(lock_path)).resume(authorized_clear=True)["outcome"] == "held"


def test_two_member_resume_persists_resuming_until_each_exact_release_readback(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    coordinator.pause()
    first = coordinator.resume(authorized_clear=True)
    assert first["outcome"] == "partial"
    interim = store.read_scope(SCOPE)["operator_intent"]
    assert interim.active and interim.resuming and set(interim.resuming_task_ids) == {"task", "task-2"}
    before_tick = tuple(board.calls)
    assert coordinator.tick()["actions_attempted"] == 0
    assert tuple(board.calls) == before_tick
    assert board.snapshots["task"].native_task["status"] == "ready"
    second = coordinator.resume(authorized_clear=True)
    assert second["outcome"] == "partial"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "verified"
    assert store.read_scope(SCOPE)["operator_intent"].active is False


def test_unsupported_stop_escalates_to_available_hold_without_claiming_stop(tmp_path):
    store, board, lock_path = setup(tmp_path, state="running", runs=({"id": "run-1", "status": "running"},))
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    result = coordinator.pause(stop=True)
    assert result["outcome"] == "partial"
    assert any(key.startswith("stop_run:task:run-1:") for key in board.calls)
    assert any(key.startswith("hold:task:") for key in board.calls)
    assert board.snapshots["task"].native_task["status"] == "blocked"


def test_resume_requires_authorized_clear_and_releases_one_held_member(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    coordinator.pause()
    denied = coordinator.resume()
    assert denied["outcome"] == "held"
    first = coordinator.resume(authorized_clear=True)
    assert first["outcome"] == "partial"
    allowed = coordinator.resume(authorized_clear=True)
    assert allowed["outcome"] == "verified"
    assert board.calls[-1].startswith("release:task:")
    assert coordinator.status()["operator_intent"].active is False
    assert coordinator.reject_late_result("late", run_generation=0).rejected


def test_cancel_persists_before_containment_and_tick_does_at_most_one_action(tmp_path):
    store, board, lock_path = setup(tmp_path)
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "ready")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    cancelled = coordinator.cancel()
    assert cancelled["cancellation_requested"] is True
    assert coordinator.reject_late_result("late").rejected
    # A fresh scope with two ready tasks proves tick's bounded action limit.
    assert len(board.calls) == 1
    assert coordinator.tick()["actions_attempted"] == 1


def test_human_edit_blocks_resume_and_lock_loss_stops_mutation(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    coordinator.pause()
    board.snapshots["task"] = snap("task", "held", digest="human-edit")
    assert coordinator.resume(authorized_clear=True)["outcome"] == "held"
    # Replacing the held lock path during a mutation fails before board action.
    board.snapshots["task"] = snap("task", "ready", digest="ready-after-human-edit")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    original = coordinator._assert_lock
    def lose_lock():
        original()
        replacement = tmp_path / "replacement.lock"; replacement.write_text("x"); replacement.chmod(0o600)
        replacement.replace(lock_path)
    coordinator._assert_lock = lose_lock
    before = len(board.calls)
    assert coordinator.tick()["outcome"] == "held"
    assert len(board.calls) == before


def test_budget_evidence_is_reported_not_reset(tmp_path):
    store, board, lock_path = setup(tmp_path)
    # Coordinator must surface the immutable store's budget evidence unchanged.
    event = {"event_id": "workflow_repairs:e1", "lineage_id": "anchor:__general_attempt__", "root_task_id": "anchor", "finding_id": "__general_attempt__", "generation": 0, "source_task_id": "task", "source_kind": "native_operation", "native_source_id": "op-1", "count": 1}
    store.record_budget_event(SCOPE, event, policy_limit=1)
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    assert coordinator.status()["budget_events"] == (event,)


def test_coordinator_rejects_extra_or_incomplete_native_readback():
    exact = snap("task", "blocked").to_dict()
    assert Coordinator._snapshot_from_readback(exact) is not None
    assert Coordinator._snapshot_from_readback({**exact, "unexpected": "data"}) is None
    missing = dict(exact)
    missing.pop("runs")
    assert Coordinator._snapshot_from_readback(missing) is None


def test_native_adapter_requires_exact_scope_and_authority_callbacks(tmp_path):
    executable = tmp_path / "hermes"; executable.write_text("fixture")
    store = EvidenceStore.open(tmp_path / "native.sqlite", create_new=True); store.migrate()
    native = HermesBoardAdapter(
        board="fixture-board", anchor_task_id="anchor", executable=str(executable),
        runner=lambda argv, **_: subprocess.CompletedProcess(argv, 0, "[]", ""),
        hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"),
        managed_member_lookup=lambda scope, task: task == "task",
        create_lock_assertion=lambda scope, anchor: None,
    )
    coordinator = Coordinator(SCOPE, board=native, store=store, lock=instance_lock(tmp_path / "native.lock"))
    # The caller-supplied no-op must not be the mutation fence.
    with pytest.raises(Exception):
        native.create_lock_assertion(SCOPE, "anchor")
    with coordinator.lock:
        native.create_lock_assertion(SCOPE, "anchor")
        with pytest.raises(ValueError):
            native.create_lock_assertion({**SCOPE, "board_id": "other"}, "anchor")
