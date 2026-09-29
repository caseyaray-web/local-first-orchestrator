from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json

import pytest

from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.budgets import BudgetPolicy, GENERAL_ATTEMPT, REVIEW_CORRECTIONS, remaining
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}


def snapshot(task_id: str, status: str, runs=(), events=(), *, assignee="implementer") -> BoardSnapshot:
    task = {"id": task_id, "status": status, "assignee": assignee}
    return BoardSnapshot(task, (), tuple(runs), (), tuple(events), (), "now", f"{task_id}:{status}:{len(runs)}:{len(events)}:{assignee}")


@dataclass
class ReviewBoard:
    task: BoardSnapshot
    source_task: BoardSnapshot | None = None
    review_task: BoardSnapshot | None = None
    is_fake: bool = True
    calls: list[str] = field(default_factory=list)

    def read_task(self, task_id: str) -> BoardSnapshot:
        if task_id == "anchor":
            return snapshot("anchor", "ready")
        if task_id == "piece" and self.source_task is not None:
            return self.source_task
        if task_id == "separate-review" and self.review_task is not None:
            return self.review_task
        assert task_id in {"piece", "separate-review", "replacement-review", "correction-task"}
        return self.task

    def read_scoped_run(self, scope, task_id: str, run_id: str):
        assert scope == SCOPE and task_id in {"piece", "separate-review", "correction-task"}
        observed = self.read_task(task_id)
        return next(run for run in observed.runs if run["id"] == run_id)

    def hold(self, *_): raise AssertionError("M2 control must not run")
    def release(self, action, task_id, _reason):
        assert task_id in {"separate-review", "replacement-review", "correction-task"}
        self.calls.append("release")
        self.task = snapshot(task_id, "ready", assignee="implementer" if task_id == "correction-task" else "local-review")
        return ActionResult(action.key, "verified", "fixture release", self.task.to_dict())
    def stop_run(self, *_): raise AssertionError("M2 control must not run")
    def request_review(self, *_):
        self.calls.append("request-review")
        raise AssertionError("only the native implementation worker may hand off review")

    def create_held(self, action, *, title, body, assignee, workspace, idempotency_key):
        self.calls.append("create-held")
        assert action.target["native_parent"] is False
        assert assignee in {"local-review", "implementer"} and workspace == "dir:/candidate"
        created_id = ("correction-task" if action.target.get("correction_of") else
                      "separate-review" if len(self.calls) == 1 else "replacement-review")
        created = snapshot(created_id, "blocked", assignee=assignee)
        if self.source_task is None:
            self.source_task = self.task
        elif action.target.get("correction_of"):
            self.review_task = self.task
        self.task = created
        return ActionResult(action.key, "verified", "fixture held create", created.to_dict())


def candidate() -> CandidateIdentity:
    return CandidateIdentity("repo", "/candidate", "base", "head", "content", "diff", "implement-run", "contract")


def next_candidate() -> CandidateIdentity:
    return CandidateIdentity("repo", "/candidate", "head", "head-2", "content-2", "diff-2", "implement-run-2", "contract")


def review(*, run_id="review-run", session_id="review-session", profile="local-review"):
    checks = [{"check_id": "tests", "outcome": "passed", "evidence": "pytest passed"}]
    return {
        "review_id": "review-1", "candidate_identity": candidate().to_dict(), "reviewer_role": "local",
        "native_review": {"task_id": "piece", "run_id": run_id, "session_id": session_id, "profile": profile},
        "checks": checks, "checks_identity": "4733546932822a33b70dbcaf3ee02fdcbe5cd6e564ba660b493f2531bf4ab8b4",
        "verdict": "approved",
        "criterion_evidence": [{"criterion_id": "tests", "outcome": "pass", "evidence": "pytest passed"}],
        "findings": [],
    }


def changes_review(*, run_id="review-run", session_id="review-session", profile="local-review"):
    evidence = review(run_id=run_id, session_id=session_id, profile=profile)
    evidence.update({
        "verdict": "changes_requested",
        "criterion_evidence": [{"criterion_id": "tests", "outcome": "fail", "evidence": "edge case missing"}],
        "findings": [{"finding_id": "finding-1", "criterion_id": "tests", "severity": "major", "summary": "cover edge case"}],
    })
    return evidence


def checks_identity(checks):
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def trusted_observation(candidate_value=None):
    if candidate_value is None:
        candidate_value = candidate()
    checks = [{"check_id": "tests", "outcome": "passed", "evidence": "pytest passed"}]
    return {
        "candidate": candidate_value.to_dict(),
        "checks": checks,
        "checks_identity": checks_identity(checks),
        "criterion_ids": ["tests"],
    }


def coordinator(tmp_path, *, git_observer=None, configured_roles=None):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True)
    store.migrate()
    store.register_member(ManagedMember("fixture-board", "anchor", "piece", "implementation", 0, ("finding-1",), "piece-1"))
    board = ReviewBoard(snapshot("piece", "running", (
        {"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {"worker_session_id": "implement-session"}},
    )))
    if git_observer is None:
        git_observer = lambda _scope: trusted_observation()
    if configured_roles is None:
        configured_roles = {"implementation_profile": "implementer", "local_review_profile": "local-review"}
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "lock"), git_observer=git_observer,
                       budget_policy=BudgetPolicy(implementation_attempts=2, review_corrections=2,
                                                  infrastructure_retries=2, workflow_repairs=2, paid_capacity=2),
                       configured_roles=configured_roles), board, store


def landed_handoff(board: ReviewBoard, marker, *, review_session="review-session"):
    board.task = snapshot("piece", "review", (
        {"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},
        {"id": "review-run", "status": "completed", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": review_session}},
    ), (
        {"kind": "review_requested", "run_id": "implement-run", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "review-run", "payload": {"run_id": "review-run", "source_status": "review"}},
    ), assignee="local-review")


def active_reviewer(board: ReviewBoard, marker, *, review_session="review-session"):
    landed_handoff(board, marker, review_session=review_session)
    board.task = snapshot("piece", "running", (
        board.task.runs[0],
        {"id": "review-run", "status": "running", "profile": "local-review",
         # Installed Hermes does not stamp this until the reviewer-owned native
         # call ends the run.  Active attribution comes from worker context.
         "metadata": None},
    ), board.task.events, assignee="local-review")


def completed_review(board: ReviewBoard, marker):
    landed_handoff(board, marker)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session"}},
    ), (
        {"kind": "review_requested", "run_id": "implement-run", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "review-run", "payload": {"run_id": "review-run", "source_status": "review"}},
        {"kind": "completed", "run_id": "review-run", "payload": {"summary": "approved"}},
    ), assignee="local-review")


def propose(controller):
    return controller.request_local_review("piece", candidate(), implementation_profile="implementer", reviewer_profile="local-review", summary="review candidate", operation_key="review-request-1")


def fresh_approval_after_changes(controller, board, store, monkeypatch, *, operation_key="review-request-2"):
    """Build a real fresh implementation/reviewer lineage after a rejection."""
    fresh = next_candidate()
    board.task = snapshot("piece", "running", (*board.task.runs,
        {"id": "implement-run-2", "status": "running", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session-2"}},
    ), board.task.events, assignee="implementer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run-2")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session-2")
    assert controller.request_local_review("piece", fresh, implementation_profile="implementer", reviewer_profile="local-review",
                                           summary="review corrected candidate", operation_key=operation_key) == {
        "outcome": "proposed", "operation_key": operation_key,
    }
    marker = next(operation.target["review_marker"] for operation in store.read_scope(SCOPE)["operations"]
                  if operation.key == operation_key)
    board.task = snapshot("piece", "done", (*board.task.runs[:-1],
        {"id": "implement-run-2", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "implement-session-2"}},
        {"id": "review-run-2", "status": "completed", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session-2"}},
    ), (*board.task.events,
        {"kind": "review_requested", "run_id": "implement-run-2", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "review-run-2", "payload": {"run_id": "review-run-2", "source_status": "review"}},
        {"kind": "completed", "run_id": "review-run-2", "payload": {"summary": "approved"}},
    ), assignee="local-review")
    approved = review(run_id="review-run-2", session_id="review-session-2")
    approved["review_id"] = "review-2"
    approved["candidate_identity"] = fresh.to_dict()
    controller.git_observer = lambda _scope: trusted_observation(fresh)
    return fresh, approved


def test_active_worker_context_reserves_handoff_before_native_terminal_session_stamp(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    # Hermes stamps worker_session_id only while kanban_request_review ends the
    # run.  The reservation must instead use the worker-only tool context.
    board.task = snapshot("piece", "running", (
        {"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {}},
    ))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    proposed = propose(controller)
    assert proposed == {"outcome": "proposed", "operation_key": "review-request-1"}
    assert board.calls == []
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    assert marker["implementation_session_id"] == "implement-session"


def test_handoff_reservation_refuses_a_non_worker_caller_without_context(tmp_path, monkeypatch):
    controller, _, _ = coordinator(tmp_path)
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="active worker-owned tool session"):
        propose(controller)


def test_approved_terminal_reviewer_run_is_held_until_the_native_card_is_done(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    proposed = propose(controller)
    assert proposed == {"outcome": "proposed", "operation_key": "review-request-1"}
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    landed_handoff(board, marker)
    assert controller.submit_review("piece", candidate(), review(), expected_profile="local-review") == {
        "outcome": "held", "reason": "review_approval_not_terminal_done",
    }
    state = store.read_scope(SCOPE)
    assert state["operations"][0].phase == "applied"
    assert state["operations"][0].readback["runs"][0]["metadata"]["worker_session_id"] == "implement-session"


def test_completed_native_reviewer_run_accepts_exact_historical_handoff(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    completed_review(board, marker)
    assert controller.submit_review("piece", candidate(), review(), expected_profile="local-review") == {
        "outcome": "accepted", "candidate": "content", "review_id": "review-1",
    }
    assert controller.tick() == {"outcome": "no-op", "actions_attempted": 0}
    assert board.calls == []


def test_completed_review_holds_when_a_later_candidate_handoff_exists(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    completed_review(board, marker)
    board.task = snapshot("piece", "done", board.task.runs, (
        *board.task.events,
        {"kind": "review_requested", "run_id": "replacement-run",
         "payload": {"implementer": "implementer", "reviewer": "local-review"}},
    ), assignee="local-review")
    assert controller.submit_review("piece", candidate(), review(), expected_profile="local-review") == {
        "outcome": "held", "reason": "verified_local_review_handoff_missing",
    }


def test_review_rejects_same_worker_session_even_with_fresh_run(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    landed_handoff(board, marker, review_session="implement-session")
    with pytest.raises(ValueError, match="fresh session"):
        controller.submit_review("piece", candidate(), review(session_id="implement-session"), expected_profile="local-review")


def test_submit_review_holds_when_no_exact_native_worker_handoff_exists(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    # The marker alone is insufficient without review_requested binding the implementation run.
    board.task = snapshot("piece", "review", (
        {"id": "implement-run", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "implement-session"}},
    ), assignee="local-review")
    held = controller.submit_review("piece", candidate(), review(), expected_profile="local-review")
    assert held == {"outcome": "held", "reason": "verified_local_review_handoff_missing"}


def test_submit_review_holds_without_trusted_git_freeze_proof(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path, git_observer=lambda _scope: None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    landed_handoff(board, marker)
    held = controller.submit_review("piece", candidate(), review(), expected_profile="local-review")
    assert held == {"outcome": "held", "reason": "trusted_git_freeze_unavailable"}


def test_submit_review_rejects_reviewer_forged_check_with_recomputed_identity(tmp_path, monkeypatch):
    legacy_observer = lambda _scope: {"candidate": candidate().to_dict(), "checks": {"candidate_content_identity": "content", "outcome": "passed"}}
    controller, board, store = coordinator(tmp_path, git_observer=legacy_observer)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    landed_handoff(board, marker)
    forged = review()
    forged["checks"] = [{"check_id": "invented", "outcome": "passed", "evidence": "reviewer says so"}]
    forged["checks_identity"] = checks_identity(forged["checks"])
    assert controller.submit_review("piece", candidate(), forged, expected_profile="local-review") == {
        "outcome": "held", "reason": "trusted_git_freeze_unavailable",
    }


def test_submit_review_requires_every_trusted_check_and_contract_criterion_to_pass(tmp_path, monkeypatch):
    observation = trusted_observation()
    observation["checks"] = [
        {"check_id": "lint", "outcome": "passed", "evidence": "lint passed"},
        {"check_id": "tests", "outcome": "passed", "evidence": "pytest passed"},
    ]
    observation["checks_identity"] = checks_identity(observation["checks"])
    observation["criterion_ids"] = ["lint", "tests"]
    controller, board, store = coordinator(tmp_path, git_observer=lambda _scope: observation)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    landed_handoff(board, marker)
    incomplete = review()
    assert controller.submit_review("piece", candidate(), incomplete, expected_profile="local-review") == {
        "outcome": "held", "reason": "trusted_git_freeze_unavailable",
    }


def test_fresh_approval_holds_until_prior_changes_has_exact_reconciled_native_correction(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    active_reviewer(board, marker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    rejected = changes_review()
    assert controller.submit_review("piece", candidate(), rejected, expected_profile="local-review")["outcome"] == "changes_requested"

    fresh, approved = fresh_approval_after_changes(controller, board, store, monkeypatch)
    assert controller.submit_review("piece", fresh, approved, expected_profile="local-review") == {
        "outcome": "held", "reason": "prior_native_correction_unreconciled",
    }


def test_repeated_same_card_changes_are_worker_proposed_then_reconciled_before_fresh_candidate_can_accept(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    controller.budget_policy = BudgetPolicy(implementation_attempts=2, review_corrections=1,
                                            infrastructure_retries=2, workflow_repairs=2, paid_capacity=2)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    active_reviewer(board, marker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    rejected = changes_review()
    rejected["findings"].append({"finding_id": "finding-2", "criterion_id": "tests", "severity": "major", "summary": "cover companion edge"})
    assert controller.submit_review("piece", candidate(), rejected, expected_profile="local-review") == {
        "outcome": "changes_requested", "candidate": "content", "review_id": "review-1",
    }
    # Native v0.21.5 does not retain worker_session_id when request-changes
    # closes the review run.  The active worker context is the only supported
    # session receipt, and must be durably bound before that native call.
    board.task = snapshot("piece", "running", (
        board.task.runs[0],
        {"id": "review-run", "status": "running", "profile": "local-review", "metadata": {}},
    ), board.task.events, assignee="local-review")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "forged-session")
    assert controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer",
                                          operation_key="changes-1") == {
        "outcome": "held", "reason": "reviewer_active_session_mismatch",
    }
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    proposed = controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer",
                                              operation_key="changes-1")
    assert proposed == {"outcome": "proposed", "operation_key": "changes-1"}
    correction = next(item for item in store.read_scope(SCOPE)["operations"] if item.key == "changes-1")
    assert correction.target["finding_ids"] == ("finding-1", "finding-2")
    assert tuple(dict(finding) for finding in correction.target["findings"]) == tuple(rejected["findings"])
    assert remaining(controller.budget_policy, store, SCOPE, REVIEW_CORRECTIONS, finding_id=GENERAL_ATTEMPT) == 0
    assert remaining(controller.budget_policy, store, SCOPE, REVIEW_CORRECTIONS, finding_id="finding-1") == 1
    # Stable retries are idempotent even after the root budget is exhausted.
    assert controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer",
                                          operation_key="changes-1") == proposed
    assert len([event for event in store.read_scope(SCOPE)["budget_events"]
                if event["event_id"].startswith("review_corrections:")]) == 1
    # A different native request cannot use a detailed finding lineage to renew
    # the already exhausted root correction generation.
    denied = controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer",
                                            operation_key="changes-2")
    assert denied["outcome"] == "held"
    assert len([event for event in store.read_scope(SCOPE)["budget_events"]
                if event["event_id"].startswith("review_corrections:")]) == 1
    # Native kanban_request_changes is reviewer-owned. Its public event—not a
    # coordinator write—proves the same card returned to its original worker.
    board.task = snapshot("piece", "ready", (
        board.task.runs[0],
        {"id": "review-run", "status": "ready", "outcome": "changes_requested", "profile": "local-review",
         "metadata": None},
    ), (*board.task.events,
        {"kind": "changes_requested", "run_id": "review-run", "payload": {
            "reason": "cover edge case", "implementer": "implementer", "reviewer": "local-review", "status": "ready",
        }},
    ), assignee="implementer")
    assert controller.reconcile_local_corrections("piece", candidate(), operation_key="changes-1") == {
        "outcome": "verified", "operation_key": "changes-1",
    }
    operation = next(item for item in store.read_scope(SCOPE)["operations"] if item.key == "changes-1")
    assert operation.target["reviewer_session_receipt"] == "review-session"
    assert controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer",
                                          operation_key="changes-1") == {"outcome": "verified", "operation_key": "changes-1"}
    assert controller.submit_review("piece", candidate(), review(), expected_profile="local-review") == {
        "outcome": "held", "reason": "review_run_verdict_conflict",
    }
    fresh = next_candidate()
    board.task = snapshot("piece", "running", (*board.task.runs,
        {"id": "implement-run-2", "status": "running", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session-2"}},
    ), board.task.events, assignee="implementer")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run-2")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session-2")
    assert controller.request_local_review("piece", fresh, implementation_profile="implementer", reviewer_profile="local-review",
                                           summary="review corrected candidate", operation_key="review-request-2") == {
        "outcome": "proposed", "operation_key": "review-request-2",
    }
    marker2 = next(operation.target["review_marker"] for operation in store.read_scope(SCOPE)["operations"]
                   if operation.key == "review-request-2")
    board.task = snapshot("piece", "done", (*board.task.runs[:-1],
        {"id": "implement-run-2", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker2, "worker_session_id": "implement-session-2"}},
        {"id": "review-run-2", "status": "completed", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session-2"}},
    ), (*board.task.events,
        {"kind": "review_requested", "run_id": "implement-run-2", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "review-run-2", "payload": {"run_id": "review-run-2", "source_status": "review"}},
        {"kind": "completed", "run_id": "review-run-2", "payload": {"summary": "approved"}},
    ), assignee="local-review")
    approved = review(run_id="review-run-2", session_id="review-session-2")
    approved["review_id"] = "review-2"
    approved["candidate_identity"] = fresh.to_dict()
    controller.git_observer = lambda _scope: trusted_observation(fresh)
    assert controller.submit_review("piece", fresh, approved, expected_profile="local-review") == {
        "outcome": "accepted", "candidate": "content-2", "review_id": "review-2",
    }


def test_premature_done_creates_one_held_separate_review_with_store_association(tmp_path):
    controller, board, store = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    first = controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review")
    second = controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review")
    assert first == second == {"outcome": "held", "task_id": "separate-review"}
    assert board.calls == ["create-held"]
    member = next(member for member in store.read_scope(SCOPE)["members"] if member.task_id == "separate-review")
    assert member.role == "local_review"
    assert member.work_association == "premature-done:piece:content"


def test_separate_premature_done_review_releases_then_accepts_only_exact_native_reviewer_evidence(tmp_path):
    """A completed source uses its bounded replacement review, not a fake handoff."""
    controller, board, store = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    assert controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review") == {
        "outcome": "held", "task_id": "separate-review",
    }
    # The explicit release is the only transition that makes a held replacement
    # eligible for the native review dispatcher.
    assert controller.release_separate_review("separate-review", candidate(), reviewer_profile="local-review") == {
        "outcome": "released", "task_id": "separate-review",
    }
    board.task = snapshot("separate-review", "done", (
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session"}},
    ), (
        {"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},
        {"kind": "completed", "run_id": "review-run", "payload": {"summary": "approved"}},
    ), assignee="local-review")
    separate_evidence = review()
    separate_evidence["native_review"] = {**separate_evidence["native_review"], "task_id": "separate-review"}
    assert controller.submit_review("separate-review", candidate(), separate_evidence, expected_profile="local-review") == {
        "outcome": "accepted", "candidate": "content", "review_id": "review-1",
    }
    assert store.read_scope(SCOPE)["reviews"] == (separate_evidence,)


def test_required_criterion_not_applicable_cannot_approve(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    propose(controller)
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    completed_review(board, marker)
    waived = review()
    waived["criterion_evidence"] = [{"criterion_id": "tests", "outcome": "not_applicable", "evidence": "waived"}]
    assert controller.submit_review("piece", candidate(), waived, expected_profile="local-review") == {
        "outcome": "held", "reason": "trusted_git_freeze_unavailable",
    }


def test_local_review_operations_fail_closed_without_configured_roles(tmp_path, monkeypatch):
    controller, _, _ = coordinator(tmp_path, configured_roles={})
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    with pytest.raises(ValueError, match="configured implementation and local-review roles"):
        propose(controller)


def test_tick_recovers_exact_done_candidate_once_without_operator_pause(tmp_path):
    controller, board, _ = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    assert controller.tick() == {"outcome": "held", "actions_attempted": 1, "task_id": "separate-review"}
    assert controller.tick() == {"outcome": "released", "actions_attempted": 1, "task_id": "separate-review"}
    assert controller.tick() == {"outcome": "no-op", "actions_attempted": 0}
    assert board.calls == ["create-held", "release"]


def test_tick_replaces_done_local_review_without_verdict_once(tmp_path):
    controller, board, _ = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    assert controller.tick()["task_id"] == "separate-review"
    board.task = snapshot("separate-review", "done", (
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session"}},
    ), assignee="local-review")
    assert controller.tick() == {"outcome": "held", "actions_attempted": 1, "task_id": "replacement-review"}
    assert controller.tick() == {"outcome": "released", "actions_attempted": 1, "task_id": "replacement-review"}
    assert board.calls == ["create-held", "create-held", "release"]

def test_tick_releases_exact_held_recovery_review_on_following_poll(tmp_path):
    controller, board, _ = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    assert controller.tick()["task_id"] == "separate-review"
    assert controller.tick() == {"outcome": "released", "actions_attempted": 1, "task_id": "separate-review"}
    assert board.calls == ["create-held", "release"]

def test_separate_approval_requires_fresh_session_from_source_implementation(tmp_path):
    controller, board, _ = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review")
    controller.release_separate_review("separate-review", candidate(), reviewer_profile="local-review")
    board.task = snapshot("separate-review", "done", (
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "implement-session"}},
    ), (
        {"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},
        {"kind": "completed", "run_id": "review-run", "payload": {}},
    ), assignee="local-review")
    evidence = review(session_id="implement-session")
    evidence["native_review"]["task_id"] = "separate-review"
    assert controller.submit_review("separate-review", candidate(), evidence, expected_profile="local-review")["outcome"] == "held"

def test_separate_review_records_changes_from_active_fresh_reviewer(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review")
    controller.release_separate_review("separate-review", candidate(), reviewer_profile="local-review")
    board.task = snapshot("separate-review", "running", (
        {"id": "review-run", "status": "running", "profile": "local-review", "metadata": None},
    ), ({"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},), assignee="local-review")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "separate-review")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    evidence = changes_review()
    evidence["native_review"]["task_id"] = "separate-review"
    assert controller.submit_review("separate-review", candidate(), evidence, expected_profile="local-review") == {
        "outcome": "changes_requested", "candidate": "content", "review_id": "review-1",
    }
    assert store.read_scope(SCOPE)["reviews"] == (evidence,)
    board.task = snapshot("separate-review", "done", (
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session"}},
    ), (
        {"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},
        {"kind": "completed", "run_id": "review-run", "payload": {}},
    ), assignee="local-review")
    conflicting = review()
    conflicting["native_review"]["task_id"] = "separate-review"
    conflicting["review_id"] = "conflicting-approval"
    assert controller.submit_review("separate-review", candidate(), conflicting,
                                    expected_profile="local-review") == {"outcome": "held", "reason": "review_run_verdict_conflict"}

def test_separate_changes_create_and_release_bounded_correction_work(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    controller.budget_policy = BudgetPolicy(implementation_attempts=2, review_corrections=1,
                                            infrastructure_retries=2, workflow_repairs=2, paid_capacity=2)
    board.task = snapshot("piece", "done", (
        {"id": "implement-run", "status": "done", "outcome": "completed", "profile": "implementer",
         "metadata": {"worker_session_id": "implement-session"}},
    ))
    controller.recover_premature_done("piece", candidate(), reviewer_profile="local-review")
    controller.release_separate_review("separate-review", candidate(), reviewer_profile="local-review")
    board.task = snapshot("separate-review", "running", (
        {"id": "review-run", "status": "running", "profile": "local-review", "metadata": None},
    ), ({"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},), assignee="local-review")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "separate-review")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    evidence = changes_review()
    evidence["native_review"]["task_id"] = "separate-review"
    evidence["criterion_evidence"].insert(0, {"criterion_id": "edge", "outcome": "fail", "evidence": "missing boundary"})
    evidence["findings"].append({"finding_id": "finding-2", "criterion_id": "edge", "severity": "major", "summary": "cover boundary"})
    controller.git_observer = lambda _scope: {**trusted_observation(), "criterion_ids": ["edge", "tests"]}
    assert controller.submit_review("separate-review", candidate(), evidence, expected_profile="local-review")["outcome"] == "changes_requested"
    board.task = snapshot("separate-review", "done", (
        {"id": "review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "review-session"}},
    ), (
        {"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}},
        {"kind": "completed", "run_id": "review-run", "payload": {}},
    ), assignee="local-review")
    assert controller.tick() == {"outcome": "held", "actions_attempted": 1, "task_id": "correction-task"}
    correction = next(m for m in store.read_scope(SCOPE)["members"] if m.task_id == "correction-task")
    assert correction.role == "implementation" and correction.finding_ids == ("finding-1", "finding-2")
    correction_create = next(item for item in store.read_scope(SCOPE)["operations"]
                             if item.effect == "create_held" and item.target.get("correction_of") == "separate-review")
    assert correction_create.target["generation"] == 1
    assert correction_create.target["finding_ids"] == ("finding-1", "finding-2")
    assert tuple(dict(finding) for finding in correction_create.target["findings"]) == tuple(evidence["findings"])
    assert remaining(controller.budget_policy, store, SCOPE, REVIEW_CORRECTIONS, finding_id=GENERAL_ATTEMPT) == 0
    # Restart retains the root reservation and still releases that authorized
    # correction; only a future admission is denied.
    database = store.path
    store.close()
    store = EvidenceStore.open(database)
    controller = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "restart-lock"),
                             git_observer=lambda _scope: {**trusted_observation(), "criterion_ids": ["edge", "tests"]},
                             budget_policy=BudgetPolicy(implementation_attempts=2, review_corrections=1,
                                                        infrastructure_retries=2, workflow_repairs=2, paid_capacity=2),
                             configured_roles={"implementation_profile": "implementer", "local_review_profile": "local-review"})
    assert controller.tick() == {"outcome": "released", "actions_attempted": 1, "task_id": "correction-task"}
    assert controller.tick() == {"outcome": "no-op", "actions_attempted": 0}
    assert board.calls == ["create-held", "release", "create-held", "release"]
    assert len([event for event in store.read_scope(SCOPE)["budget_events"]
                if event["event_id"].startswith("review_corrections:")]) == 1
    # The correction is a real managed implementation card, not merely a
    # finding record: it can enter the same native worker-owned review loop.
    fresh = CandidateIdentity("repo", "/candidate", "head", "head-2", "content-2", "diff-2", "correction-run", "contract")
    board.task = snapshot("correction-task", "running", (
        {"id": "correction-run", "status": "running", "profile": "implementer", "metadata": None},
    ), assignee="implementer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "correction-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "correction-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "correction-session")
    controller.git_observer = lambda _scope: trusted_observation(fresh)
    assert controller.request_local_review("correction-task", fresh, implementation_profile="implementer",
                                           reviewer_profile="local-review", summary="review correction",
                                           operation_key="correction-review") == {"outcome": "proposed", "operation_key": "correction-review"}
    marker = next(item.target["review_marker"] for item in store.read_scope(SCOPE)["operations"]
                  if item.key == "correction-review")
    board.task = snapshot("correction-task", "done", (
        {"id": "correction-run", "status": "completed", "outcome": "review_requested", "profile": "implementer",
         "metadata": {"local_first_review": marker, "worker_session_id": "correction-session"}},
        {"id": "correction-review-run", "status": "done", "outcome": "completed", "profile": "local-review",
         "metadata": {"worker_session_id": "fresh-review-session"}},
    ), (
        {"kind": "review_requested", "run_id": "correction-run", "payload": {"implementer": "implementer", "reviewer": "local-review"}},
        {"kind": "claimed", "run_id": "correction-review-run", "payload": {"source_status": "review"}},
        {"kind": "completed", "run_id": "correction-review-run", "payload": {}},
    ), assignee="local-review")
    approval = review(run_id="correction-review-run", session_id="fresh-review-session")
    approval["review_id"] = "correction-approval"
    approval["candidate_identity"] = fresh.to_dict()
    approval["native_review"]["task_id"] = "correction-task"
    assert controller.submit_review("correction-task", fresh, approval, expected_profile="local-review")["outcome"] == "accepted"
    assert controller.tick() == {"outcome": "no-op", "actions_attempted": 0}


def test_prior_changes_reconciliation_normalizes_full_reversed_findings_but_rejects_altered_content(tmp_path, monkeypatch):
    controller, board, store = coordinator(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "piece")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "implement-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "implement-session")
    assert propose(controller)["outcome"] == "proposed"
    marker = store.read_scope(SCOPE)["operations"][0].target["review_marker"]
    active_reviewer(board, marker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "review-run")
    monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
    rejected = changes_review()
    rejected["findings"].append({"finding_id": "finding-2", "criterion_id": "tests", "severity": "major", "summary": "second"})
    rejected["findings"].reverse()
    assert controller.submit_review("piece", candidate(), rejected, expected_profile="local-review")["outcome"] == "changes_requested"
    assert controller.request_corrections("piece", candidate(), rejected, implementation_profile="implementer", operation_key="reversed")["outcome"] == "proposed"
    board.task = snapshot("piece", "ready", (board.task.runs[0], {"id": "review-run", "status": "ready", "outcome": "changes_requested", "profile": "local-review", "metadata": None}),
                          (*board.task.events, {"kind": "changes_requested", "run_id": "review-run", "payload": {"reason": "cover edge case", "implementer": "implementer", "reviewer": "local-review", "status": "ready"}}), assignee="implementer")
    assert controller.reconcile_local_corrections("piece", candidate(), operation_key="reversed")["outcome"] == "verified"
    state = store.read_scope(SCOPE)
    assert controller._prior_changes_are_reconciled("piece", state)
    operation = next(item for item in state["operations"] if item.key == "reversed")
    altered = replace(operation, target={**operation.target, "findings": ({**operation.target["findings"][0], "summary": "altered"}, *operation.target["findings"][1:])})
    assert not controller._prior_changes_are_reconciled("piece", {**state, "operations": tuple(altered if item.key == "reversed" else item for item in state["operations"])})
