"""M5 restart proof for the supported fixture stop_run capability.

Production HermesBoardAdapter remains explicitly unsupported; this isolated
adapter proves Coordinator's durable exact-run protocol without implying native
CLI support.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "m5-stop", "anchor_task_id": "anchor"}


class ProcessDeath(BaseException): pass


def snapshot(status: str, revision: int) -> BoardSnapshot:
    return BoardSnapshot({"id": "work", "status": status}, (),
        ({"id": "run-1", "status": status, "stop_supported": True},), (), (), (), "now", f"work:{status}:{revision}")


@dataclass
class StopBoard:
    current: BoardSnapshot
    sends: list[str]

    is_fake = True

    def read_task(self, task_id: str) -> BoardSnapshot:
        assert task_id == "work"
        return self.current

    def hold(self, *_args, **_kwargs):
        raise AssertionError("hold is not part of the stop fixture")

    def release(self, *_args, **_kwargs):
        raise AssertionError("release is not part of the stop fixture")

    def stop_run(self, action: Action, task_id: str, run_id: str, reason: str) -> ActionResult:
        assert (task_id, run_id) == ("work", "run-1") and reason
        self.sends.append(action.key)
        self.current = snapshot("stopped", 1)
        return ActionResult(action.key, "verified", "fixture worker stopped", self.current.to_dict())

    def verify_effect(self, action: Action) -> ActionResult:
        return ActionResult(action.key, "verified" if self.current.runs[0]["status"] == "stopped" else "unknown",
                            "exact stop readback", self.current.to_dict())


def open_controller(path: Path, board: StopBoard, tmp_path: Path) -> tuple[Coordinator, EvidenceStore]:
    store = EvidenceStore.open(path, create_new=not path.exists()); store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember("m5-stop", "anchor", "work", "implementation", 0, (), "stop-work"))
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "stop.lock"),
                       budget_policy=BudgetPolicy(2, 2, 2, 2, 2)), store


@pytest.mark.parametrize("boundary", ("before_claim", "after_claim_before_effect", "after_effect_before_ack", "after_ack"))
def test_m5_supported_stop_run_crash_reopen_never_resends_unknown_and_requires_terminal_readback(tmp_path: Path, monkeypatch, boundary: str) -> None:
    database = tmp_path / "stop.sqlite"
    board = StopBoard(snapshot("running", 0), [])
    controller, store = open_controller(database, board, tmp_path)
    action = Action("stop-run-1", SCOPE, {"task_id": "work", "run_id": "run-1"}, "stop_run", board.current.digest)
    original_begin, original_ack = store.begin_effect_attempt, store.ack_effect
    if boundary == "before_claim":
        monkeypatch.setattr(store, "begin_effect_attempt", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("before stop claim")))
    elif boundary == "after_claim_before_effect":
        monkeypatch.setattr(store, "begin_effect_attempt", lambda *a, **k: (original_begin(*a, **k), (_ for _ in ()).throw(ProcessDeath("after stop claim")))[1])
    elif boundary == "after_effect_before_ack":
        monkeypatch.setattr(store, "ack_effect", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("after stop effect")))
    else:
        monkeypatch.setattr(store, "ack_effect", lambda *a, **k: (original_ack(*a, **k), (_ for _ in ()).throw(ProcessDeath("after stop ack")))[1])
    try:
        with pytest.raises(ProcessDeath):
            with controller.lock:
                controller._apply(action)
        op = next(op for op in store.read_scope(SCOPE)["operations"] if op.key == action.key)
        assert board.sends == ([action.key] if boundary in {"after_effect_before_ack", "after_ack"} else [])
        assert op.phase == ("pending" if boundary == "before_claim" else "unknown" if boundary != "after_ack" else "applied")
    finally:
        store.close()

    resumed, reopened = open_controller(database, board, tmp_path)
    try:
        if boundary == "before_claim":
            with resumed.lock:
                assert resumed._apply(action).outcome == "verified"
        elif boundary == "after_claim_before_effect":
            assert resumed.reconcile()["outcome"] == "partial"
            assert board.current.runs[0]["status"] == "running"  # unknown is never declared terminal
        elif boundary == "after_effect_before_ack":
            resumed.reconcile()
        else:
            # Exact terminal readback changed the task digest; _apply rejects a
            # stale replay rather than sending again. The durable receipt remains
            # the sole source of completed-stop authority.
            with resumed.lock:
                assert resumed._apply(action).outcome == "unsupported"
        expected_sends = [] if boundary == "after_claim_before_effect" else [action.key]
        assert board.sends == expected_sends
        phase = next(op for op in reopened.read_scope(SCOPE)["operations"] if op.key == action.key).phase
        assert phase == ("unknown" if boundary == "after_claim_before_effect" else "applied")
    finally:
        reopened.close()
