"""M5 restart proofs using the current EvidenceStore/M3 contract boundary.

No test imports the legacy Ledger/MicroTicket comment outbox.  Comment delivery,
review handoff, and reviewer-owned request-changes are represented as durable
EvidenceStore operations plus externally observed native snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "m5-review", "anchor_task_id": "anchor"}


class ProcessDeath(BaseException):
    """Escapes ordinary recovery handlers, as an abrupt process loss would."""


def candidate() -> CandidateIdentity:
    return CandidateIdentity("repo", "/candidate", "base", "head", "content", "diff", "implement-run", "contract")


def snapshot(task_id: str, status: str, runs=(), events=(), comments=(), *, assignee="implementer", revision=0) -> BoardSnapshot:
    return BoardSnapshot({"id": task_id, "status": status, "assignee": assignee}, (), tuple(runs), tuple(comments),
                         tuple(events), (), "fixture-now", f"{task_id}:{status}:{revision}:{len(runs)}:{len(events)}:{len(comments)}")


@dataclass
class ReviewBoard:
    task: BoardSnapshot
    calls: list[str] = field(default_factory=list)
    is_fake: bool = True

    def read_task(self, task_id: str) -> BoardSnapshot:
        assert task_id in {"anchor", "piece"}
        return snapshot("anchor", "ready") if task_id == "anchor" else self.task

    def read_scoped_run(self, scope, task_id: str, run_id: str):
        assert scope == SCOPE and task_id == "piece"
        return next(run for run in self.task.runs if run["id"] == run_id)

    def request_review(self, *_args, **_kwargs):
        self.calls.append("request-review")
        raise AssertionError("only the native implementation worker may hand off review")

    def hold(self, *_args, **_kwargs):
        raise AssertionError("review provenance never performs coordinator containment")

    def release(self, *_args, **_kwargs):
        raise AssertionError("review provenance never performs coordinator release")

    def stop_run(self, *_args, **_kwargs):
        raise AssertionError("review provenance never stops a native worker")


def review_changes() -> dict[str, object]:
    return {
        "review_id": "review-1", "candidate_identity": candidate().to_dict(), "reviewer_role": "local",
        "native_review": {"task_id": "piece", "run_id": "review-run", "session_id": "review-session", "profile": "local-review"},
        "checks": [{"check_id": "tests", "outcome": "passed", "evidence": "pytest passed"}],
        "checks_identity": "4733546932822a33b70dbcaf3ee02fdcbe5cd6e564ba660b493f2531bf4ab8b4",
        "verdict": "changes_requested", "criterion_evidence": [{"criterion_id": "tests", "outcome": "fail", "evidence": "missing edge"}],
        "findings": [{"finding_id": "finding-1", "criterion_id": "tests", "severity": "major", "summary": "cover edge case"}],
    }


def _open(database: Path, board: ReviewBoard, tmp_path: Path) -> tuple[Coordinator, EvidenceStore]:
    store = EvidenceStore.open(database, create_new=not database.exists())
    store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember("m5-review", "anchor", "piece", "implementation", 0, ("finding-1",), "piece-1"))
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "m5-review.lock"),
                       git_observer=lambda _scope: {"candidate": candidate().to_dict(), "checks": [{"check_id": "tests", "outcome": "passed", "evidence": "pytest passed"}], "checks_identity": "4733546932822a33b70dbcaf3ee02fdcbe5cd6e564ba660b493f2531bf4ab8b4", "criterion_ids": ["tests"]},
                       budget_policy=BudgetPolicy(2, 2, 2, 2, 2),
                       configured_roles={"implementation_profile": "implementer", "local_review_profile": "local-review"}), store


def _seed(tmp_path: Path, monkeypatch) -> tuple[Coordinator, ReviewBoard, EvidenceStore]:
    board = ReviewBoard(snapshot("piece", "running", ({"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {"worker_session_id": "implement-session"}},)))
    controller, store = _open(tmp_path / "review.sqlite", board, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    assert controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review", summary="review candidate", operation_key="review-request-1") == {"outcome": "proposed", "operation_key": "review-request-1"}
    marker = next(op.target["review_marker"] for op in store.read_scope(SCOPE)["operations"] if op.key == "review-request-1")
    board.task = snapshot("piece", "running", (
        {"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer", "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},
        {"id": "review-run", "status": "running", "profile": "local-review", "metadata": {}},
    ), ({"kind": "review_requested", "run_id": "implement-run", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "review"}}), assignee="local-review", revision=1)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    rejected = review_changes()
    assert controller.submit_review("piece", candidate(), rejected, expected_profile="local-review")["outcome"] == "changes_requested"
    return controller, board, store


def _native_changes(board: ReviewBoard) -> None:
    board.task = snapshot("piece", "ready", (
        board.task.runs[0], {"id": "review-run", "status": "ready", "outcome": "changes_requested", "profile": "local-review", "metadata": None},
    ), (*board.task.events, {"kind": "changes_requested", "run_id": "review-run", "payload": {"reason": "cover edge case", "implementer": "implementer", "reviewer": "local-review", "status": "ready"}}), assignee="implementer", revision=2)


@pytest.mark.parametrize("boundary", ("before_intent", "after_reservation", "after_worker_transition", "post_ack_reopen"))
def test_m5_reviewer_owned_request_changes_restart_provenance(tmp_path: Path, monkeypatch, boundary: str) -> None:
    controller, board, store = _seed(tmp_path, monkeypatch)
    database = store.path
    rejected = review_changes()
    original_reserve, original_ack = store.reserve_budgeted_repair_operation, store.ack_effect
    if boundary == "before_intent":
        monkeypatch.setattr(store, "reserve_budgeted_repair_operation", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("before correction reservation")))
    elif boundary == "after_reservation":
        def reserve_then_die(*args, **kwargs):
            original_reserve(*args, **kwargs)
            raise ProcessDeath("after correction reservation")
        monkeypatch.setattr(store, "reserve_budgeted_repair_operation", reserve_then_die)
    try:
        if boundary in {"before_intent", "after_reservation"}:
            with pytest.raises(ProcessDeath):
                controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer", operation_key="changes-1")
        else:
            assert controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer", operation_key="changes-1") == {"outcome": "proposed", "operation_key": "changes-1"}
            _native_changes(board)  # external reviewer-owned native transition
            if boundary == "post_ack_reopen":
                def ack_then_die(*args, **kwargs):
                    original_ack(*args, **kwargs)
                    raise ProcessDeath("after correction acknowledgement")
                monkeypatch.setattr(store, "ack_effect", ack_then_die)
            with pytest.raises(ProcessDeath):
                controller.reconcile_local_corrections("piece", candidate(), operation_key="changes-1") if boundary == "post_ack_reopen" else (_ for _ in ()).throw(ProcessDeath("after worker transition before reconciliation"))
        assert board.calls == []
    finally:
        store.close()

    resumed, reopened = _open(database, board, tmp_path)
    try:
        if boundary == "before_intent":
            assert resumed.request_corrections("piece", candidate(), rejected, implementation_profile="implementer", operation_key="changes-1") == {"outcome": "proposed", "operation_key": "changes-1"}
            _native_changes(board)
        if boundary == "after_reservation":
            assert resumed.reconcile_local_corrections("piece", candidate(), operation_key="changes-1") == {"outcome": "held", "reason": "verified_native_correction_missing"}
            _native_changes(board)
        assert resumed.reconcile_local_corrections("piece", candidate(), operation_key="changes-1") == {"outcome": "verified", "operation_key": "changes-1"}
        state = reopened.read_scope(SCOPE)
        operations = [op for op in state["operations"] if op.effect == "request_changes"]
        assert len(operations) == 1 and operations[0].phase == "applied"
        assert operations[0].target["reviewer_session_receipt"] == "review-session"
        assert len([event for event in state["budget_events"] if event["event_id"] == "review_corrections:changes-1"]) == 1
        assert board.calls == []
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ("before_claim", "after_reservation", "after_worker_handoff", "post_ack_reopen"))
def test_m5_worker_owned_reopen_review_handoff_restart_provenance(tmp_path: Path, monkeypatch, boundary: str) -> None:
    board = ReviewBoard(snapshot("piece", "running", ({"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {"worker_session_id": "implement-session"}},)))
    controller, store = _open(tmp_path / "handoff.sqlite", board, tmp_path)
    database = store.path
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece"); monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run"); monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    original_reserve, original_ack = store.reserve_operation, store.ack_effect
    if boundary == "before_claim":
        monkeypatch.setattr(store, "reserve_operation", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("before handoff reservation")))
    elif boundary == "after_reservation":
        def reserve_then_die(*args, **kwargs):
            original_reserve(*args, **kwargs); raise ProcessDeath("after handoff reservation")
        monkeypatch.setattr(store, "reserve_operation", reserve_then_die)
    try:
        if boundary in {"before_claim", "after_reservation"}:
            with pytest.raises(ProcessDeath):
                controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review", summary="reopen corrected candidate", operation_key="reopen-review-1")
        else:
            assert controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review", summary="reopen corrected candidate", operation_key="reopen-review-1")["outcome"] == "proposed"
            marker = next(op.target["review_marker"] for op in store.read_scope(SCOPE)["operations"] if op.key == "reopen-review-1")
            board.task = snapshot("piece", "review", ({"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer", "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},), ({"kind": "review_requested", "run_id": "implement-run", "payload": {"implementer": "implementer", "reviewer": "local-review"}},), assignee="local-review", revision=1)
            if boundary == "post_ack_reopen":
                def ack_then_die(*args, **kwargs):
                    original_ack(*args, **kwargs); raise ProcessDeath("after handoff acknowledgement")
                monkeypatch.setattr(store, "ack_effect", ack_then_die)
                with pytest.raises(ProcessDeath):
                    controller._verified_handoff("piece", candidate(), "local-review")
            else:
                raise ProcessDeath("after native worker handoff before acknowledgement")
        assert board.calls == []
    except ProcessDeath:
        if boundary == "after_worker_handoff":
            pass
        else:
            raise
    finally:
        store.close()

    resumed, reopened = _open(database, board, tmp_path)
    try:
        if boundary in {"before_claim", "after_reservation"}:
            assert resumed.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review", summary="reopen corrected candidate", operation_key="reopen-review-1")["outcome"] == "proposed"
            assert resumed._verified_handoff("piece", candidate(), "local-review") is None
        else:
            assert resumed._verified_handoff("piece", candidate(), "local-review") is not None
        operations = [op for op in reopened.read_scope(SCOPE)["operations"] if op.effect == "request_review"]
        assert len(operations) == 1 and board.calls == []
    finally:
        reopened.close()
