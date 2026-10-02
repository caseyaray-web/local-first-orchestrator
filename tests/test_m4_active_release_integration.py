from __future__ import annotations

import subprocess
from pathlib import Path

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import Action, ActionResult, BoardSnapshot, CandidateIdentity, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.git_adapter import GitWorktreeAdapter

SCOPE = {"board_id": "board", "anchor_task_id": "root"}


def snapshot(task_id, status, digest, *, assignee="implementer"):
    return BoardSnapshot({"id": task_id, "status": status, "assignee": assignee}, (), (), (), (), (), "now", digest)


class Board:
    is_fake = True
    def __init__(self):
        self.cards = {"root": snapshot("root", "blocked", "root-hold", assignee="planner"), "piece": snapshot("piece", "blocked", "held")}
        self.releases = 0
    def read_task(self, task_id): return self.cards[task_id]
    def hold(self, *_): raise AssertionError("not used")
    def stop_run(self, *_): raise AssertionError("not used")
    def _native_marker(self, action): return "marker:" + action.key
    def release(self, action, task_id, reason):
        self.releases += 1
        assert task_id == "piece" and "marker:" in reason
        self.cards[task_id] = snapshot(task_id, "ready", "ready", assignee="implementer")
        return ActionResult(action.key, "verified", "released", self.cards[task_id].to_dict())
    def verify_effect(self, action):
        card = self.cards[action.target["task_id"]]
        return ActionResult(action.key, "verified" if card.native_task["status"] == "ready" else "unknown", "read", card.to_dict())


def git(path, *args):
    return subprocess.run(("git", *args), cwd=path, text=True, capture_output=True, check=True).stdout.strip()


def test_active_release_and_serial_git_integration_smoke(tmp_path):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    board = Board()
    store.register_member(ManagedMember("board", "root", "root", "root", 0, (), "opaque-root"))
    store.register_member(ManagedMember("board", "root", "piece", "implementation", 0, (), "piece-association"))
    create = OperationIntent("create", SCOPE, {"plan_id":"plan", "ticket_id":"ticket"}, "create_held", "root-hold", {}, "verified", board.cards["piece"].to_dict(), {}, "applied")
    store.reserve_operation(create)
    ctl = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "lock"), budget_policy=BudgetPolicy(1, 1, 1, 1, 1), configured_roles={"implementation_profile":"implementer", "local_review_profile":"reviewer"})
    released = ctl.release_active_piece("plan", "ticket")
    assert released["outcome"] == "released" and released["actions_attempted"] == 1 and board.releases == 1
    assert ctl.release_active_piece("plan", "ticket")["actions_attempted"] == 0 and board.releases == 1

    repo = tmp_path / "repo"; repo.mkdir(); git(repo, "init", "-q"); git(repo, "config", "user.name", "T"); git(repo, "config", "user.email", "t@example.invalid")
    (repo / "a.txt").write_text("base\n"); git(repo, "add", "."); git(repo, "commit", "-qm", "base"); base = git(repo, "rev-parse", "HEAD")
    adapter = GitWorktreeAdapter(repo, tmp_path / "attempts"); attempt = adapter.create_attempt("ticket", 1, base)
    (attempt.path / "a.txt").write_text("changed\n"); git(attempt.path, "add", "."); git(attempt.path, "commit", "-qm", "piece"); head = git(attempt.path, "rev-parse", "HEAD")
    adapter.resolve_execution_base("tranche", base)
    candidate = CandidateIdentity("repo", str(attempt.path), base, head, "content-1", "diff-1", "run-1", "contract-1")
    store.record_candidate(SCOPE, candidate)
    review = {"review_id":"review-1", "candidate_identity":candidate.to_dict(), "reviewer_role":"local", "native_review":{"task_id":"piece", "run_id":"review-run", "session_id":"session", "profile":"reviewer"}, "checks":[{"check_id":"smoke", "outcome":"passed", "evidence":"ok"}], "checks_identity":"", "verdict":"approved", "criterion_evidence":[{"criterion_id":"c1", "outcome":"pass", "evidence":"ok"}], "findings":[]}
    import hashlib, json
    review["checks_identity"] = hashlib.sha256(json.dumps(review["checks"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    store.record_review(SCOPE, candidate, review)
    store.read_accepted_plan = lambda scope, plan_id: {"active_tranche":{"tranche_id":"tranche", "ordinal":0}, "base_sha":base}
    ctl._integrated_piece_authority = lambda state, plan_id, ticket_id, candidate: None
    integrated = ctl.integrate_active_piece("plan", "ticket", candidate, review_id="review-1", git_adapter=adapter)
    assert integrated["outcome"] == "integrated" and integrated["integration_head_after"] == head
    assert adapter.existing_execution_base("tranche", base) == head
    assert ctl.integrate_active_piece("plan", "ticket", candidate, review_id="review-1", git_adapter=adapter)["outcome"] == "integrated"
    stale = CandidateIdentity("repo", str(attempt.path), base, head, "content-changed", "diff-changed", "run-2", "contract-1")
    stale_review = {**review, "review_id":"review-2", "candidate_identity":stale.to_dict(), "native_review":{"task_id":"piece", "run_id":"review-run-2", "session_id":"session-2", "profile":"reviewer"}}
    store.record_review(SCOPE, stale, stale_review)
    blocked = ctl.integrate_active_piece("plan", "ticket", stale, review_id="review-2", git_adapter=adapter)
    assert blocked["outcome"] == "held" and blocked["reason"] == "candidate_base_does_not_match_expected_tranche_head"
    assert adapter.existing_execution_base("tranche", base) == head
    store.close()
