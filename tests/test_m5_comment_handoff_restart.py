"""M5 close/open proofs for comment and the initial worker-owned review handoff.

These fixtures use the current EvidenceStore, Coordinator, Action, and M3 worker
context APIs only.  A native worker transition is injected as board observation;
the coordinator never calls a board review-handoff method.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, ManagedMember, NATIVE_COMMENT_AUTHOR
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from tests.test_hermes_board_adapter import FakeKanban
from tests.test_local_review_loop import ReviewBoard, SCOPE, candidate, snapshot, trusted_observation


class ProcessDeath(BaseException):
    """Model an abrupt process loss that normal coordinator recovery cannot catch."""


class CommentBoard:
    """Current-contract fixture board with marker-visible comment readback."""

    is_fake = True

    def __init__(self) -> None:
        self.comments: list[dict[str, str]] = []
        self.comment_calls = 0
        self.task = self._snapshot()

    def _snapshot(self) -> BoardSnapshot:
        return BoardSnapshot(
            {"id": "piece", "status": "ready", "assignee": "implementer"}, (), (),
            tuple(self.comments), (), (), "fixture-now", f"piece:ready:{len(self.comments)}",
        )

    def read_task(self, task_id: str) -> BoardSnapshot:
        if task_id == "anchor":
            return snapshot("anchor", "ready")
        assert task_id == "piece"
        return self.task

    def hold(self, *_args, **_kwargs) -> ActionResult:
        raise AssertionError("comment proof must not contain work")

    def release(self, *_args, **_kwargs) -> ActionResult:
        raise AssertionError("comment proof must not release work")

    def stop_run(self, *_args, **_kwargs) -> ActionResult:
        raise AssertionError("comment proof must not stop work")

    def comment(self, action: Action, task_id: str, text: str) -> ActionResult:
        assert task_id == "piece"
        self.comment_calls += 1
        self.comments.append({"author": NATIVE_COMMENT_AUTHOR, "body": text})
        self.task = self._snapshot()
        return ActionResult(action.key, "verified", "fixture comment", self.task.to_dict())

    def verify_effect(self, action: Action) -> ActionResult:
        marker = action.target["marker"]
        exact = [item for item in self.comments if item == {"author": NATIVE_COMMENT_AUTHOR, "body": marker}]
        return ActionResult(action.key, "verified" if len(exact) == 1 else "unknown", "fixture marker readback", self.task.to_dict())


def _open_comment(database: Path, board: CommentBoard, tmp_path: Path) -> tuple[Coordinator, EvidenceStore]:
    store = EvidenceStore.open(database, create_new=not database.exists())
    store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember(SCOPE["board_id"], SCOPE["anchor_task_id"], "piece", "implementation", 0, (), "piece-1"))
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "comment.lock"),
                       budget_policy=BudgetPolicy(2, 2, 2, 2, 2)), store


def _comment_action(board: CommentBoard) -> Action:
    return Action("comment-1", SCOPE, {"task_id": "piece", "author": NATIVE_COMMENT_AUTHOR, "marker": "<!-- local-first-action:comment-1 --> M5 durable comment"},
                  "comment", board.read_task("piece").digest)


def _open_native_comment(database: Path, transport: FakeKanban, tmp_path: Path) -> tuple[Coordinator, EvidenceStore, HermesBoardAdapter]:
    """Use the production adapter over its supported fake CLI transport."""
    executable = tmp_path / "native-hermes"
    executable.write_text("fixture")
    board = HermesBoardAdapter(
        board=SCOPE["board_id"], anchor_task_id=SCOPE["anchor_task_id"], executable=str(executable), runner=transport,
        hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"), timeout_seconds=3,
        managed_member_lookup=lambda scope, task_id: scope == SCOPE and task_id == "piece",
        create_lock_assertion=lambda *_: None, claim_create_attempt=lambda *_: None,
    )
    store = EvidenceStore.open(database, create_new=not database.exists())
    store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember(SCOPE["board_id"], SCOPE["anchor_task_id"], "piece", "implementation", 0, (), "piece-1"))
    return (Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "native-comment.lock"),
                        budget_policy=BudgetPolicy(2, 2, 2, 2, 2)), store, board)


def test_m5_native_adapter_comment_crash_reopens_and_reconciles_exact_author_without_resend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinator and real adapter must agree on the one native author."""
    transport = FakeKanban()
    transport.add("anchor", status="ready")
    transport.add("piece", status="ready", assignee="implementer")
    controller, store, board = _open_native_comment(tmp_path / "native-comment.sqlite", transport, tmp_path)
    database = store.path
    action = Action("native-comment-1", SCOPE,
                    {"task_id": "piece", "author": NATIVE_COMMENT_AUTHOR,
                     "marker": "<!-- local-first-action:native-comment-1 --> M5 native adapter comment"},
                    "comment", board.read_task("piece").digest)
    try:
        monkeypatch.setattr(store, "ack_effect", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("after native comment before acknowledgement")))
        with controller.lock:
            with pytest.raises(ProcessDeath):
                controller._apply(action)
    finally:
        store.close()

    resumed, reopened, _ = _open_native_comment(database, transport, tmp_path)
    try:
        assert resumed.reconcile()["outcome"] == "verified"
        state = reopened.read_scope(SCOPE)
        operation = next(item for item in state["operations"] if item.key == action.key)
        assert operation.phase == "applied"
        assert transport.tasks["piece"]["comments"] == [{"author": NATIVE_COMMENT_AUTHOR, "body": action.target["marker"]}]
        assert [call[4] for call in transport.calls].count("comment") == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ("before_begin", "after_claim_before_board_comment", "after_board_effect_before_ack", "post_ack_reopen"))
def test_m5_comment_restart_boundaries_close_open_no_resend_and_no_budget_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    board = CommentBoard()
    controller, store = _open_comment(tmp_path / "comment.sqlite", board, tmp_path)
    database, action = store.path, _comment_action(board)
    original_begin, original_comment, original_ack = store.begin_effect_attempt, board.comment, store.ack_effect
    try:
        if boundary == "before_begin":
            monkeypatch.setattr(store, "begin_effect_attempt", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("before durable effect claim")))
        elif boundary == "after_claim_before_board_comment":
            monkeypatch.setattr(board, "comment", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("after claim before board comment")))
        elif boundary == "after_board_effect_before_ack":
            def comment_then_die(*args, **kwargs):
                original_comment(*args, **kwargs)
                raise ProcessDeath("after board comment before acknowledgement")
            monkeypatch.setattr(board, "comment", comment_then_die)
        else:
            def ack_then_die(*args, **kwargs):
                original_ack(*args, **kwargs)
                raise ProcessDeath("after comment acknowledgement")
            monkeypatch.setattr(store, "ack_effect", ack_then_die)
        with controller.lock:
            with pytest.raises(ProcessDeath):
                controller._apply(action)
    finally:
        store.close()

    resumed, reopened = _open_comment(database, board, tmp_path)
    try:
        if boundary == "before_begin":
            with resumed.lock:
                assert resumed._apply(action).outcome == "verified"
            assert board.comment_calls == 1
        elif boundary == "after_claim_before_board_comment":
            assert resumed.reconcile()["outcome"] == "partial"
            assert board.comment_calls == 0
        else:
            if boundary == "after_board_effect_before_ack":
                assert resumed.reconcile()["outcome"] == "verified"
            else:
                with resumed.lock:
                    assert resumed._apply(action).outcome == "verified"
            assert board.comment_calls == 1
        state = reopened.read_scope(SCOPE)
        operation = next(item for item in state["operations"] if item.key == action.key)
        assert operation.effect == "comment"
        assert operation.phase == ("unknown" if boundary == "after_claim_before_board_comment" else "applied")
        assert state["budget_events"] == ()
    finally:
        reopened.close()


def _open_handoff(database: Path, board: ReviewBoard, tmp_path: Path) -> tuple[Coordinator, EvidenceStore]:
    store = EvidenceStore.open(database, create_new=not database.exists())
    store.migrate()
    if not store.read_scope(SCOPE)["members"]:
        store.register_member(ManagedMember(SCOPE["board_id"], SCOPE["anchor_task_id"], "piece", "implementation", 0, ("finding-1",), "piece-1"))
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "handoff.lock"),
                       git_observer=lambda _scope: trusted_observation(), budget_policy=BudgetPolicy(2, 2, 2, 2, 2),
                       configured_roles={"implementation_profile": "implementer", "local_review_profile": "local-review"}), store


def _initial_implementation_board() -> ReviewBoard:
    return ReviewBoard(snapshot("piece", "running", (
        {"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {}},
    )))


def _native_initial_handoff(board: ReviewBoard, marker: object) -> None:
    board.task = snapshot("piece", "review", (
        {"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},
    ), ({"kind": "review_requested", "run_id": "implement-run",
         "payload": {"implementer": "implementer", "reviewer": "local-review"}},), assignee="local-review")


@pytest.mark.parametrize("boundary", ("before_intent", "reserved_before_worker_transition", "after_worker_handoff_before_ack", "post_ack_reopen"))
def test_m5_initial_worker_owned_request_review_restart_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    board = _initial_implementation_board()
    controller, store = _open_handoff(tmp_path / "handoff.sqlite", board, tmp_path)
    database = store.path
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    original_reserve, original_ack = store.reserve_operation, store.ack_effect
    try:
        if boundary == "before_intent":
            monkeypatch.setattr(store, "reserve_operation", lambda *_a, **_k: (_ for _ in ()).throw(ProcessDeath("before handoff intent")))
        elif boundary == "reserved_before_worker_transition":
            def reserve_then_die(*args, **kwargs):
                original_reserve(*args, **kwargs)
                raise ProcessDeath("after handoff reservation")
            monkeypatch.setattr(store, "reserve_operation", reserve_then_die)
        if boundary in {"before_intent", "reserved_before_worker_transition"}:
            with pytest.raises(ProcessDeath):
                controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review",
                                                summary="initial implementation review", operation_key="initial-review-1")
        else:
            assert controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review",
                                                   summary="initial implementation review", operation_key="initial-review-1")["outcome"] == "proposed"
            marker = next(item.target["review_marker"] for item in store.read_scope(SCOPE)["operations"] if item.key == "initial-review-1")
            _native_initial_handoff(board, marker)
            if boundary == "post_ack_reopen":
                def ack_then_die(*args, **kwargs):
                    original_ack(*args, **kwargs)
                    raise ProcessDeath("after native handoff acknowledgement")
                monkeypatch.setattr(store, "ack_effect", ack_then_die)
                with pytest.raises(ProcessDeath):
                    controller._verified_handoff("piece", candidate(), "local-review")
            else:
                # The worker transition is external/native.  Crash before the
                # coordinator can read and acknowledge that public evidence.
                with pytest.raises(ProcessDeath):
                    (_ for _ in ()).throw(ProcessDeath("after worker handoff before acknowledgement"))
    finally:
        store.close()

    resumed, reopened = _open_handoff(database, board, tmp_path)
    try:
        if boundary in {"before_intent", "reserved_before_worker_transition"}:
            assert resumed.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review",
                                                summary="initial implementation review", operation_key="initial-review-1")["outcome"] == "proposed"
            marker = next(item.target["review_marker"] for item in reopened.read_scope(SCOPE)["operations"] if item.key == "initial-review-1")
            _native_initial_handoff(board, marker)
        assert resumed._verified_handoff("piece", candidate(), "local-review") is not None
        state = reopened.read_scope(SCOPE)
        operations = [item for item in state["operations"] if item.effect == "request_review"]
        assert len(operations) == 1 and operations[0].phase == "applied"
        assert operations[0].target["review_marker"]["implementation_session_id"] == "implement-session"
        assert len([event for event in state["budget_events"] if event["event_id"].startswith("review_")]) == 0
        assert board.calls == []
    finally:
        reopened.close()
