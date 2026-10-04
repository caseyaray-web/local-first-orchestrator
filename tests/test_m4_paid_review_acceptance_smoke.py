from __future__ import annotations

import hashlib
import json
from pathlib import Path

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore

SCOPE = {"board_id": "board", "anchor_task_id": "root"}


def snap(task_id, status, digest, assignee="implementer", *, runs=(), events=()):
    return BoardSnapshot({"id": task_id, "status": status, "assignee": assignee}, (), tuple(runs), (), tuple(events), (), "now", digest)


class Board:
    is_fake = True
    def __init__(self):
        self.cards = {"root": snap("root", "blocked", "anchor", "planner")}
        self.created = 0
    def read_task(self, task_id): return self.cards[task_id]
    def hold(self, *_): raise AssertionError("unused")
    def _native_marker(self, action): return "marker:" + action.key
    def release(self, action, task_id, _reason):
        self.cards[task_id] = snap(task_id, "ready", task_id + "-ready", self.cards[task_id].native_task["assignee"])
        return ActionResult(action.key, "verified", "released", self.cards[task_id].to_dict())
    def stop_run(self, *_): raise AssertionError("unused")
    def create_held(self, action, *, title, body, assignee, workspace, idempotency_key):
        assert action.target["native_parent"] is False
        self.created += 1
        task_id = ("paid-review" if self.created == 1 else "correction" if self.created == 2 else "paid-review-2")
        self.cards[task_id] = snap(task_id, "blocked", task_id, assignee)
        return ActionResult(action.key, "verified", "held", self.cards[task_id].to_dict())
    def verify_effect(self, action):
        return ActionResult(action.key, "unknown", "not created", None)
    def read_scoped_run(self, scope, task_id, run_id):
        card = self.cards[task_id]
        return next(dict(run) for run in card.runs if str(run["id"]) == str(run_id))


class Git:
    def __init__(self, root, base, head): self.primary_checkout, self.base, self.head = root, base, head
    def existing_execution_base(self, tranche_id, base):
        assert tranche_id == "tranche" and base == self.base
        return self.head
    def freeze_candidate(self, _path, *, base_sha, expected_head_sha):
        from local_first_orchestrator.contracts import CandidateIdentity
        return CandidateIdentity("synthetic", str(self.primary_checkout), base_sha, expected_head_sha, "synthetic-content", "synthetic-diff", "synthetic-run", "synthetic-contract")
    def advance_integration_head(self, tranche_id, expected, candidate):
        assert tranche_id == "tranche" and expected == self.head
        self.head = candidate
        return candidate


def check_identity(checks):
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_paid_review_checks_correction_and_exact_acceptance_smoke(tmp_path, monkeypatch):
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    board = Board(); base = "a" * 40; head = "b" * 40; git = Git(tmp_path, base, head)
    store.register_member(ManagedMember("board", "root", "root", "root", 0, (), "root"))
    integration = OperationIntent("integrated", SCOPE, {"plan_id":"plan", "ticket_id":"piece", "candidate":{"head_sha":head}}, "git_integrate", base, {}, "verified", {"integration_head_before":base, "integration_head_after":head}, {}, "applied")
    store.reserve_operation(integration)
    store.read_accepted_plan = lambda scope, plan_id: {"active_tranche":{"tranche_id":"tranche", "ordinal":0}, "base_sha":base}
    def runner(context):
        assert context["head_sha"] in {head, "c" * 40}
        return {"head_sha":context["head_sha"], "checks":[{"check_id":"smoke", "command":"python -c pass", "exit_code":0, "output_sha256":"sha256:" + context["head_sha"]}]}
    ctl = Coordinator(SCOPE, board=board, store=store, lock=instance_lock(tmp_path / "lock"), budget_policy=BudgetPolicy(2, 2, 2, 2, 2), configured_roles={"implementation_profile":"implementer", "local_review_profile":"local", "paid_review_profile":"paid"}, combined_check_runner=runner)
    from types import SimpleNamespace
    ctl._accepted_tranche_targets = lambda _plan: (SimpleNamespace(ticket_id="piece"),)
    request = ctl.prepare_paid_integrated_review("plan", git_adapter=git)
    assert request["outcome"] == "held" and request["review_task_id"] == "paid-review" and board.created == 1
    candidate = dict(next(op for op in store.read_scope(SCOPE)["operations"] if op.key == request["review_request_key"]).readback["candidate"])
    checks = [{"check_id":"smoke", "outcome":"passed", "evidence":"trusted"}]
    review = {"review_id":"paid-ok", "candidate_identity":candidate, "reviewer_role":"paid", "native_review":{"task_id":"paid-review", "run_id":"paid-run", "session_id":"paid-session", "profile":"paid"}, "checks":checks, "checks_identity":check_identity(checks), "verdict":"approved", "criterion_evidence":[{"criterion_id":"c", "outcome":"pass", "evidence":"trusted"}], "findings":[]}
    # A held paid-review request has no worker authority until this separately
    # budgeted admission moves the exact card to ready.
    assert ctl.submit_paid_integrated_review("plan", review, git_adapter=git)["outcome"] == "held"
    paid_release = ctl.release_paid_integrated_review("plan", git_adapter=git)
    assert paid_release["outcome"] == "released" and paid_release["task_id"] == "paid-review", paid_release
    assert len([event for event in store.read_scope(SCOPE)["budget_events"]
                if event["event_id"].startswith("paid_capacity:")]) == 1
    board.cards["paid-review"] = snap("paid-review", "ready", "paid-review-ready", "paid", runs=(
        {"id":"paid-run", "task_id":"paid-review", "profile":"paid", "status":"completed", "metadata":{"worker_session_id":"paid-session"}},
    ))
    assert ctl.submit_paid_integrated_review("plan", {**review, "native_review": {**review["native_review"], "session_id": "wrong-session"}}, git_adapter=git)["outcome"] == "held"
    board.cards["paid-review"] = snap("paid-review", "done", "paid-review", "paid", runs=(
        {"id":"paid-run", "task_id":"paid-review", "profile":"paid", "status":"completed", "metadata":{"worker_session_id":"paid-session"}},
    ))
    assert ctl.submit_paid_integrated_review("plan", review, git_adapter=git)["outcome"] == "approved"
    # Synthetic fixture evidence drives the coordinator only; no provider is called.
    changed = {**review, "review_id": "paid-changes", "verdict": "changes_requested",
               "criterion_evidence": [{"criterion_id": "c", "outcome": "fail", "evidence": "synthetic"}],
               "findings": [{"finding_id": "f-1", "criterion_id": "c", "severity": "major", "summary": "synthetic finding"}]}
    board.cards["paid-review"] = snap("paid-review", "blocked", "paid-review", "paid", runs=(
        {"id":"paid-run", "task_id":"paid-review", "profile":"paid", "status":"running", "metadata":{"worker_session_id":"paid-session"}},
    ))
    assert ctl.submit_paid_integrated_review("plan", changed, git_adapter=git)["outcome"] == "changes_requested"
    correction = ctl.prepare_paid_correction("plan", "paid-changes", git_adapter=git)
    assert correction["outcome"] == "held" and correction["correction_task_id"] == "correction"
    released = ctl.release_paid_correction("plan", "paid-changes", git_adapter=git)
    assert released["outcome"] == "released" and released["task_id"] == "correction"
    from local_first_orchestrator.contracts import CandidateIdentity
    correction_candidate = CandidateIdentity("synthetic", str(tmp_path), head, "c" * 40, "content-c", "diff-c", "impl-c", "contract-c")
    local_checks = [{"check_id": "smoke", "outcome": "passed", "evidence": "synthetic"}]
    local_review = {"review_id":"local-correction", "candidate_identity":correction_candidate.to_dict(), "reviewer_role":"local", "native_review":{"task_id":"correction", "run_id":"local-run", "session_id":"local-session", "profile":"local"}, "checks":local_checks, "checks_identity":check_identity(local_checks), "verdict":"approved", "criterion_evidence":[{"criterion_id":"c", "outcome":"pass", "evidence":"synthetic"}], "findings":[]}
    # Same-card local review is a worker-owned handoff, not a synthetic
    # completed run supplied by the caller.  Retain the implementation run,
    # its marker/event, and the reviewer claim/completion as native-shaped
    # transport evidence before submitting the structured verdict.
    implementation_session = "impl-session"
    board.cards["correction"] = snap("correction", "running", "correction-running", "implementer", runs=(
        {"id":"impl-c", "task_id":"correction", "profile":"implementer", "status":"running", "metadata":{"worker_session_id":implementation_session}},
    ))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "correction")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "impl-c")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", SCOPE["board_id"])
    monkeypatch.setenv("HERMES_SESSION_ID", implementation_session)
    ctl.git_observer = lambda _scope: {"candidate": correction_candidate.to_dict(), "checks": local_checks,
                                       "checks_identity": check_identity(local_checks), "criterion_ids": ["c"]}
    assert ctl.request_local_review("correction", correction_candidate, implementation_profile="implementer",
                                    reviewer_profile="local", summary="synthetic candidate", operation_key="local-handoff-c")["outcome"] == "proposed"
    marker = next(op.target["review_marker"] for op in store.read_scope(SCOPE)["operations"] if op.key == "local-handoff-c")
    board.cards["correction"] = snap("correction", "done", "correction-done", "local", runs=(
        {"id":"impl-c", "task_id":"correction", "profile":"implementer", "status":"completed", "outcome":"review_requested", "metadata":{"local_first_review":marker, "worker_session_id":implementation_session}},
        {"id":"local-run", "task_id":"correction", "profile":"local", "status":"completed", "outcome":"completed", "metadata":{"worker_session_id":"local-session"}},
    ), events=(
        {"kind":"review_requested", "run_id":"impl-c", "payload":{"implementer":"implementer", "reviewer":"local"}},
        {"kind":"claimed", "run_id":"local-run", "payload":{"run_id":"local-run", "source_status":"review"}},
        {"kind":"completed", "run_id":"local-run", "payload":{"summary":"approved"}},
    ))
    ctl.finalization_observer = lambda _scope, _candidate: {
        "candidate": correction_candidate.to_dict(), "checks": local_checks,
        "checks_identity": check_identity(local_checks), "criterion_ids": ["c"]}
    assert ctl.finalize_local_review("local-handoff-c")["outcome"] == "finalized"
    assert ctl.submit_local_review("plan", correction["operation_key"], correction_candidate,
                                   {**local_review, "native_review": {**local_review["native_review"], "session_id": "wrong-session"}})["outcome"] == "held"
    assert ctl.submit_local_review("plan", correction["operation_key"], correction_candidate, local_review)["outcome"] == "approved"
    assert ctl.integrate_active_piece("plan", correction["operation_key"], correction_candidate, review_id="local-correction", git_adapter=git)["outcome"] == "integrated"
    request2 = ctl.prepare_paid_integrated_review("plan", git_adapter=git)
    assert request2.get("outcome") == "held" and request2.get("head_sha") == correction_candidate.head_sha, request2
    candidate2 = dict(next(op for op in store.read_scope(SCOPE)["operations"] if op.key == request2["review_request_key"]).readback["candidate"])
    paid2 = {"review_id":"paid-correction-ok", "candidate_identity":candidate2, "reviewer_role":"paid", "native_review":{"task_id":request2["review_task_id"], "run_id":"paid-run-2", "session_id":"paid-session-2", "profile":"paid"}, "checks":checks, "checks_identity":check_identity(checks), "verdict":"approved", "criterion_evidence":[{"criterion_id":"c", "outcome":"pass", "evidence":"synthetic"}], "findings":[]}
    assert ctl.release_paid_integrated_review("plan", git_adapter=git)["outcome"] == "released"
    board.cards[request2["review_task_id"]] = snap(request2["review_task_id"], "done", "paid-review-2", "paid", runs=(
        {"id":"paid-run-2", "task_id":request2["review_task_id"], "profile":"paid", "status":"completed", "metadata":{"worker_session_id":"paid-session-2"}},
    ))
    assert ctl.submit_paid_integrated_review("plan", paid2, git_adapter=git)["outcome"] == "approved"
    assert ctl.accept_tranche("plan", "paid-correction-ok", git_adapter=git)["outcome"] == "accepted"
    store.close()


def test_separately_accepted_successor_is_materialized_held_then_one_piece_released(tmp_path, monkeypatch):
    """Public successor APIs perform the real held-create and separately budgeted release."""
    import dataclasses
    from tests.test_m4_active_piece_preparation import _captured_piece_result
    from tests.test_m4_active_tranche_materialization import fixture
    from local_first_orchestrator.planning_coordinator import evidence_payload, request_payload

    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    board = Board(); base = "a" * 40; head = "b" * 40; git = Git(tmp_path, base, head)
    store.register_member(ManagedMember("board", "root", "root", "root", 0, (), "root"))
    req0, proposal0, _ = fixture()
    request = dataclasses.replace(req0, board_id="board", anchor_id="root", base_sha=head)
    plan = dataclasses.replace(proposal0.plan, plan_id="successor-plan")
    proposal = dataclasses.replace(proposal0, request_identity=request.identity, plan=plan)
    evidence = evidence_payload(request, proposal, planner_task_id="planner", planner_run_id="run", planner_session_id="session", planner_profile="paid-planner")
    route = {"implementation_profile":"implementer", "workspace":str(tmp_path)}
    successor = {"schema_version":1, "plan_id":"successor-plan", "request_identity":request.identity,
                 "proposal_hash":proposal.proposal_hash, "plan_contract_hash":plan.contract_hash, "planner":evidence["planner"],
                 "repository_identity":request.repository_identity, "base_sha":head, "snapshot_hash":request.snapshot_hash,
                 "root_contract_hash":request.root_contract_hash, "active_tranche":{"tranche_id":"TR-A", "ordinal":0}, "route":route}
    successor["acceptance_identity"] = "sha256:" + hashlib.sha256(json.dumps(successor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    parent = {**successor, "plan_id":"parent-plan", "base_sha":base, "active_tranche":{"tranche_id":"tranche", "ordinal":0}, "acceptance_identity":"parent-accepted"}
    store.read_accepted_plan = lambda scope, plan_id: parent if plan_id == "parent-plan" else successor if plan_id == "successor-plan" else (_ for _ in ()).throw(KeyError(plan_id))
    store.read_plan = lambda scope, plan_id: evidence if plan_id == "successor-plan" else (_ for _ in ()).throw(KeyError(plan_id))
    store.read_planning_request = lambda scope, request_id=None: {"request":request_payload(request)}
    acceptance = OperationIntent("acceptance", SCOPE, {"kind":"tranche_acceptance_v1", "plan_id":"parent-plan", "tranche_id":"tranche", "head_sha":head, "review_id":"paid-ok", "check_operation_key":"checks"}, "tranche_accept", head, {}, "verified", {"head_sha":head, "review_id":"paid-ok", "successor_authorized":True}, {}, "applied")
    store.reserve_operation(acceptance)
    def create(action, **_kwargs):
        task_id = "successor-" + action.target["ticket_id"]
        card = snap(task_id, "blocked", task_id + "-held", "implementer")
        board.cards[task_id] = card
        return _captured_piece_result(action, card)
    monkeypatch.setattr(board, "create_held", create)
    def verify(action):
        task_id = "successor-" + action.target["ticket_id"]
        return _captured_piece_result(action, board.cards[task_id])
    monkeypatch.setattr(board, "verify_effect", verify)
    class TrackingLock(type(instance_lock(tmp_path / "tracking-lock"))):
        acquisitions = 0
        def acquire(self):
            type(self).acquisitions += 1
            return super().acquire()
    lock = TrackingLock(tmp_path / "lock")
    ctl = Coordinator(SCOPE, board=board, store=store, lock=lock, budget_policy=BudgetPolicy(2, 2, 2, 2, 2), configured_roles={"implementation_profile":"implementer", "local_review_profile":"local", "paid_review_profile":"paid"}, planning_workspace=str(tmp_path), planning_observer=lambda scope: {"request":request_payload(request)})
    materialized = ctl.materialize_accepted_successor("parent-plan", "successor-plan", "acceptance", git_adapter=git)
    assert materialized["outcome"] == "held" and materialized["materialization"]["total"] == 2, materialized
    assert TrackingLock.acquisitions == 1
    ticket_id = materialized["materialization"]["pieces"][0]["ticket_id"]
    released = ctl.release_accepted_successor_piece("parent-plan", "successor-plan", "acceptance", ticket_id, git_adapter=git)
    assert released["outcome"] == "released" and released["task_id"] == "successor-" + ticket_id and released["actions_attempted"] == 1
    assert len([event for event in store.read_scope(SCOPE)["budget_events"] if event["event_id"].startswith("implementation_attempts:")]) == 1
    store.close()
