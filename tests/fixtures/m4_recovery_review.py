"""Small synthetic board used only by retained M4 recovery regressions."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor"}


def snapshot(task_id, status, runs=(), events=(), *, assignee="implementer"):
    task = {"id": task_id, "status": status, "assignee": assignee}
    return BoardSnapshot(task, (), tuple(runs), (), tuple(events), (), "fixture", f"{task_id}:{status}:{len(runs)}:{len(events)}:{assignee}")


@dataclass
class Board:
    task: BoardSnapshot
    source: BoardSnapshot | None = None
    calls: list[str] = field(default_factory=list)
    is_fake: bool = True

    def read_task(self, task_id):
        if task_id == "anchor":
            return snapshot("anchor", "ready")
        if task_id == "piece" and self.source is not None:
            return self.source
        return self.task

    def read_scoped_run(self, scope, task_id, run_id):
        assert scope == SCOPE
        return next(run for run in self.read_task(task_id).runs if run["id"] == run_id)

    def hold(self, *_args):
        raise AssertionError("recovery review must not use a hold effect")

    def stop_run(self, *_args):
        raise AssertionError("recovery review must not stop a run")

    def create_held(self, action, *, assignee, **_kwargs):
        self.calls.append("create-held")
        assert action.target["native_parent"] is False
        if self.source is None:
            self.source = self.task
        task_id = "separate-review" if self.calls.count("create-held") == 1 else "replacement-review"
        self.task = snapshot(task_id, "blocked", assignee=assignee)
        return ActionResult(action.key, "verified", "created", self.task.to_dict())

    def release(self, action, task_id, _reason):
        self.calls.append("release")
        self.task = snapshot(task_id, "ready", assignee="local-review")
        return ActionResult(action.key, "verified", "released", self.task.to_dict())


def candidate():
    return CandidateIdentity("repo", "/candidate", "base", "head", "content", "diff", "implement-run", "contract")


def observation(value=None):
    value = value or candidate()
    checks = [{"check_id": "tests", "outcome": "passed", "evidence": "fixture"}]
    return {"candidate": value.to_dict(), "checks": checks,
            "checks_identity": hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "criterion_ids": ["tests"]}


def review(task_id="separate-review", *, verdict="approved"):
    checks = [{"check_id": "tests", "outcome": "passed", "evidence": "fixture"}]
    return {"review_id": "review-1", "candidate_identity": candidate().to_dict(), "reviewer_role": "local",
            "native_review": {"task_id": task_id, "run_id": "review-run", "session_id": "review-session", "profile": "local-review"},
            "checks": checks, "checks_identity": hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "verdict": verdict, "criterion_evidence": [{"criterion_id": "tests", "outcome": "pass", "evidence": "fixture"}], "findings": []}


def completed_review(task_id="separate-review", *, status="done", claimed=True, completed=True):
    events = []
    if claimed:
        events.append({"kind": "claimed", "run_id": "review-run", "payload": {"source_status": "ready"}})
    if completed:
        events.append({"kind": "completed", "run_id": "review-run", "payload": {"summary": "fixture"}})
    return snapshot(task_id, status, ({"id": "review-run", "status": "completed", "outcome": "completed", "profile": "local-review", "metadata": {"worker_session_id": "review-session"}},), events, assignee="local-review")


def _coordinator(tmp_path, board, store):
    return Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "lock"), git_observer=lambda _scope: observation(),
        budget_policy=BudgetPolicy(2, 2, 2, 2, 2), configured_roles={"implementation_profile": "implementer", "local_review_profile": "local-review"})


def coordinator(tmp_path):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True)
    store.migrate()
    store.register_member(ManagedMember("fixture-board", "anchor", "piece", "implementation", 0, (), "piece"))
    board = Board(snapshot("piece", "running", ({"id": "implement-run", "status": "running", "profile": "implementer", "metadata": {"worker_session_id": "implementation-session"}},)))
    return _coordinator(tmp_path, board, store), board, store


def reopened_coordinator(tmp_path, board):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=False)
    store.migrate()
    return _coordinator(tmp_path, board, store), store


def create_and_release(ctl, board):
    board.task = snapshot("piece", "done", ({"id": "implement-run", "status": "completed", "outcome": "completed", "profile": "implementer", "metadata": {"worker_session_id": "implementation-session"}},))
    assert ctl.tick() == {"outcome": "held", "actions_attempted": 1, "task_id": "separate-review"}
    assert ctl.tick() == {"outcome": "released", "actions_attempted": 1, "task_id": "separate-review"}
