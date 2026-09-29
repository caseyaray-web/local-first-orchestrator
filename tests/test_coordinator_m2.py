from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
from typing import Any
import pytest

from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.budgets import BudgetPolicy, GENERAL_ATTEMPT, REVIEW_CORRECTIONS, admit_repair_operation
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
    unknown_stop_keys: set[str] = field(default_factory=set)
    markers: set[str] = field(default_factory=set)

    def read_task(self, task_id: str) -> BoardSnapshot:
        return self.snapshots[task_id]

    def hold(self, action: Action, task_id: str, reason: str) -> ActionResult:
        self.calls.append(action.key)
        if action.key in self.unknown_keys:
            return ActionResult(action.key, "unknown", "lost reply", None)
        before = self.read_task(task_id)
        if any(run.get("status") in {"active", "claimed", "running", "stopping"} for run in before.runs):
            return ActionResult(action.key, "partial", "running work cannot be safely held", before.to_dict())
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
        if action.key in self.unknown_stop_keys:
            return ActionResult(action.key, "unknown", "lost stop reply", None)
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


def test_ready_members_pause_then_release_each_from_their_proven_hold_readback(tmp_path):
    store, board, lock_path = setup(tmp_path)
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "ready")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))

    assert coordinator.pause()["outcome"] == "partial"
    assert coordinator.tick()["outcome"] == "verified"
    assert {board.snapshots[task].native_task["status"] for task in ("task", "task-2")} == {"blocked"}
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "verified"


def test_resume_rejects_human_edit_after_ready_members_were_held(tmp_path):
    store, board, lock_path = setup(tmp_path)
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "ready")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))

    coordinator.pause()
    coordinator.tick()
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    board.snapshots["task-2"] = snap("task-2", "held", digest="human-edit-after-hold")

    assert coordinator.resume(authorized_clear=True) == {
        "outcome": "held", "reason": "unsafe_human_edits", "actions_attempted": 0,
    }


@dataclass
class NativeRunner:
    tasks: dict[str, dict[str, Any]]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv, **_):
        args = tuple(argv[4:])
        self.calls.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks[args[1]]), "")
        if args[0] == "runs":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.tasks[args[1]]["runs"]), "")
        if args[0] == "block":
            task = self.tasks[args[1]]
            prior = task["task"]["status"]
            task["task"]["status"] = "blocked"
            task["comments"].append({"author": "hermes", "body": f"BLOCKED: {args[2]}"})
            task["events"].append({"kind": "blocked", "payload": {"reason": args[2], "kind": "needs_input", "source_status": prior, "recurrences": 1}})
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "unsupported")


def test_real_adapter_partial_running_hold_sends_no_block_and_tick_contains_later_target(tmp_path):
    def task(task_id, status, runs=()):
        return {"task": {"id": task_id, "status": status}, "parents": [], "runs": list(runs), "comments": [], "events": [], "attachments": []}

    runner = NativeRunner({
        "anchor": task("anchor", "blocked"),
        "task": task("task", "running", ({"id": "run-1", "status": "running"},)),
        "task-2": task("task-2", "ready"),
    })
    executable = tmp_path / "hermes"; executable.write_text("fixture")
    store = EvidenceStore.open(tmp_path / "native.sqlite", create_new=True); store.migrate()
    store.register_member(ManagedMember("fixture-board", "anchor", "task", "implementation", 0, (), "member-1"))
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    native = HermesBoardAdapter(
        board="fixture-board", anchor_task_id="anchor", executable=str(executable), runner=runner,
        hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"),
        managed_member_lookup=lambda _scope, task_id: task_id in {"task", "task-2"},
    )
    coordinator = Coordinator(SCOPE, board=native, store=store, lock=instance_lock(tmp_path / "native.lock"))

    paused = coordinator.pause()
    assert paused["outcome"] == "partial" and paused["active_workers"] == ("run-1",)
    assert not any(call[0] == "block" and call[1] == "task" for call in runner.calls)
    advanced = coordinator.tick()
    assert advanced["outcome"] == "partial"
    assert any(call[0] == "block" and call[1] == "task-2" for call in runner.calls)
    assert runner.tasks["task-2"]["task"]["status"] == "blocked"
    assert store.pending_operations(SCOPE)


def test_exhausted_finding_lineage_denies_resume_without_charging_safety_controls(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    store.connection.execute("DELETE FROM managed_members")
    store.connection.commit()
    store.register_member(ManagedMember("fixture-board", "anchor", "task", "implementation", 0, ("f1",), "member"))
    event = {"event_id": "workflow_repairs:f1-cap", "lineage_id": "anchor:f1", "root_task_id": "anchor", "finding_id": "f1", "generation": 0, "source_task_id": "task", "source_kind": "native_operation", "native_source_id": "f1-cap", "count": 1}
    store.record_budget_event(SCOPE, event, policy_limit=1)
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path), budget_policy=BudgetPolicy(1, 1, 1, 1, 1))

    assert coordinator.pause()["outcome"] == "verified"
    before_events = store.read_scope(SCOPE)["budget_events"]
    assert coordinator.resume(authorized_clear=True) == {"outcome": "held", "reason": "budget_exhausted", "actions_attempted": 0}
    assert store.read_scope(SCOPE)["budget_events"] == before_events



def test_reserved_final_correction_releases_after_pause_when_root_budget_is_exhausted(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    policy = BudgetPolicy(1, 1, 1, 0, 1)
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path), budget_policy=policy)
    correction = Action("separate-review-correction:reserved", SCOPE, {
        "task_id": "task", "correction_of": "separate-review", "association": "separate-review-correction:separate-review:content:1",
    }, "create_held", board.snapshots["task"].digest)
    event = {"event_id": f"{REVIEW_CORRECTIONS}:{correction.key}",
             "lineage_id": f"anchor:{GENERAL_ATTEMPT}", "root_task_id": "anchor", "finding_id": GENERAL_ATTEMPT,
             "generation": 0, "source_task_id": "task", "source_kind": "native_operation",
             "native_source_id": correction.key, "count": 1}
    reserved = admit_repair_operation(policy, store, SCOPE, coordinator._intent_for(correction), event)
    store.ack_effect(SCOPE, reserved.key, readback=board.snapshots["task"].to_dict())

    assert coordinator.pause()["outcome"] == "verified"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    assert board.snapshots["task"].native_task["status"] == "ready"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "verified"
    final_intent = store.read_scope(SCOPE)["operator_intent"]
    assert final_intent.active is False
    assert final_intent.resuming is False


@pytest.mark.parametrize("unsafe", ("unknown_effect", "human_edit"))
def test_completed_reserved_release_refuses_finalization_on_unknown_effect_or_drift(tmp_path, unsafe):
    store, board, lock_path = setup(tmp_path, state="held")
    policy = BudgetPolicy(1, 1, 1, 0, 1)
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path), budget_policy=policy)
    correction = Action("separate-review-correction:reserved", SCOPE, {
        "task_id": "task", "correction_of": "separate-review", "association": "separate-review-correction:separate-review:content:1",
    }, "create_held", board.snapshots["task"].digest)
    event = {"event_id": f"{REVIEW_CORRECTIONS}:{correction.key}",
             "lineage_id": f"anchor:{GENERAL_ATTEMPT}", "root_task_id": "anchor", "finding_id": GENERAL_ATTEMPT,
             "generation": 0, "source_task_id": "task", "source_kind": "native_operation",
             "native_source_id": correction.key, "count": 1}
    reserved = admit_repair_operation(policy, store, SCOPE, coordinator._intent_for(correction), event)
    store.ack_effect(SCOPE, reserved.key, readback=board.snapshots["task"].to_dict())

    assert coordinator.pause()["outcome"] == "verified"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    if unsafe == "unknown_effect":
        unresolved = Action("hold:task:unresolved", SCOPE, {"task_id": "task"}, "hold", "unresolved")
        store.reserve_operation(coordinator._intent_for(unresolved))
        store.begin_effect_attempt(SCOPE, unresolved.key)
        expected_reason = "unknown_effects"
    else:
        board.snapshots["task"] = snap("task", "ready", digest="human-edit-after-release")
        expected_reason = "unsafe_human_edits"

    assert coordinator.resume(authorized_clear=True) == {
        "outcome": "held", "reason": expected_reason, "actions_attempted": 0,
    }
    intent = store.read_scope(SCOPE)["operator_intent"]
    assert intent.active is True
    assert intent.resuming is True


def test_exhausted_finding_lineage_stops_continued_resume_without_charging_controls(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    store.connection.execute("DELETE FROM managed_members")
    store.connection.commit()
    for task_id, association in (("task", "member-1"), ("task-2", "member-2")):
        store.register_member(ManagedMember("fixture-board", "anchor", task_id, "implementation", 0, ("f1",), association))
    board.snapshots["task-2"] = snap("task-2", "held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path), budget_policy=BudgetPolicy(1, 1, 1, 1, 1))

    assert coordinator.pause()["outcome"] == "verified"
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    event = {"event_id": "workflow_repairs:f1-cap", "lineage_id": "anchor:f1", "root_task_id": "anchor", "finding_id": "f1", "generation": 0, "source_task_id": "task-2", "source_kind": "native_operation", "native_source_id": "f1-cap", "count": 1}
    store.record_budget_event(SCOPE, event, policy_limit=1)
    before_events = store.read_scope(SCOPE)["budget_events"]

    assert coordinator.reconcile()["outcome"] == "verified"
    assert coordinator.resume(authorized_clear=True) == {"outcome": "held", "reason": "budget_exhausted", "actions_attempted": 0}
    assert board.snapshots["task-2"].native_task["status"] == "blocked"
    assert store.read_scope(SCOPE)["budget_events"] == before_events


@pytest.mark.parametrize("unsafe", ("human_edit", "active_run", "unknown_effect", "budget"))
def test_continued_resume_does_not_release_second_member_after_any_gate_fails(tmp_path, unsafe):
    store, board, lock_path = setup(tmp_path, state="held")
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    coordinator.pause()
    assert coordinator.resume(authorized_clear=True)["outcome"] == "partial"
    assert board.snapshots["task"].native_task["status"] == "ready"
    if unsafe == "budget":
        coordinator.budget_policy = BudgetPolicy(0, 0, 0, 0, 0)
    elif unsafe == "human_edit":
        board.snapshots["task"] = snap("task", "ready", digest="human-edit-after-first-release")
    elif unsafe == "active_run":
        board.snapshots["task"] = snap("task", "ready", ({"id": "late-run", "status": "running"},), digest="active-after-first-release")
    elif unsafe == "unknown_effect":
        unknown = Action("hold:task-2:unresolved", SCOPE, {"task_id": "task-2"}, "hold", "unresolved")
        store.reserve_operation(coordinator._intent_for(unknown))
        store.begin_effect_attempt(SCOPE, unknown.key)

    before = tuple(board.calls)
    result = coordinator.resume(authorized_clear=True)

    assert result["outcome"] == "held"
    assert not any(key.startswith("release:task-2:") for key in board.calls[len(before):])
    assert board.snapshots["task-2"].native_task["status"] == "blocked"
    assert store.read_scope(SCOPE)["operator_intent"].active is True


def test_resume_without_an_active_intent_fails_closed_without_native_effect(tmp_path):
    store, board, lock_path = setup(tmp_path, state="held")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))

    assert coordinator.resume(authorized_clear=True)["outcome"] == "held"
    assert board.calls == []


def test_unknown_stop_on_one_member_does_not_block_bounded_hold_of_later_member(tmp_path):
    store, board, lock_path = setup(tmp_path, state="running", runs=({"id": "run-1", "status": "running"},))
    store.register_member(ManagedMember("fixture-board", "anchor", "task-2", "implementation", 0, (), "member-2"))
    board.snapshots["task-2"] = snap("task-2", "running", ({"id": "run-2", "status": "running"},))
    board.unknown_stop_keys.add("stop_run:task:run-1:task:running:1")
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))

    assert coordinator.pause(stop=True)["outcome"] == "partial"
    assert board.snapshots["task"].native_task["status"] == "running"
    first_stop = "stop_run:task:run-1:task:running:1"
    assert board.calls.count(first_stop) == 1
    assert coordinator.tick()["outcome"] == "partial"
    assert board.snapshots["task-2"].native_task["status"] == "running"
    assert board.calls.count(first_stop) == 1


def test_known_unsupported_exact_stop_reports_running_work_without_false_hold(tmp_path):
    store, board, lock_path = setup(
        tmp_path, state="running", runs=({"id": "run-1", "status": "running", "stop_supported": False},),
    )
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))

    assert coordinator.pause(stop=True)["outcome"] == "partial"
    assert not any(key.startswith("stop_run:") for key in board.calls)
    assert board.snapshots["task"].native_task["status"] == "running"


def test_unsupported_stop_attempts_hold_without_claiming_containment(tmp_path):
    store, board, lock_path = setup(tmp_path, state="running", runs=({"id": "run-1", "status": "running"},))
    coordinator = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(lock_path))
    result = coordinator.pause(stop=True)
    assert result["outcome"] == "partial"
    assert any(key.startswith("stop_run:task:run-1:") for key in board.calls)
    assert any(key.startswith("hold:task:") for key in board.calls)
    assert board.snapshots["task"].native_task["status"] == "running"


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
