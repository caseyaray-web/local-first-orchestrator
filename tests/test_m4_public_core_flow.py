from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import ActionResult, BoardSnapshot, CandidateIdentity, PauseIntent
from local_first_orchestrator.git_adapter import GitWorktreeAdapter
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.recovery import RecoveryIssue
from tests.test_m4_active_piece_dependency_authority import _batch_fixture
from tests.test_m4_accepted_dependency_preparation import _frozen_raw_cards
from tests.test_m4_active_tranche_materialization import fixture as successor_fixture
from local_first_orchestrator.planning_coordinator import evidence_payload, request_payload


def _git(path, *args):
    return subprocess.run(("git", *args), cwd=path, check=True, text=True, capture_output=True).stdout.strip()


def _identity(checks):
    return hashlib.sha256(json.dumps(checks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(card, *, status=None, assignee=None, comments=None, events=None):
    native = dict(card.native_task)
    if status is not None:
        native["status"] = status
    if assignee is not None:
        native["assignee"] = assignee
    return BoardSnapshot(native, card.parents, card.runs, tuple(comments if comments is not None else card.comments),
                         tuple(events if events is not None else card.events), card.attachments, card.observed_at,
                         hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest())


def _review(review_id, candidate, *, role, task_id, run_id, session, verdict="approved", findings=None):
    checks = [{"check_id": "smoke", "outcome": "passed", "evidence": "synthetic fixture"}]
    criterion = [{"criterion_id": "AC-1", "outcome": "pass" if verdict == "approved" else "fail", "evidence": "synthetic fixture"}]
    return {"review_id": review_id, "candidate_identity": candidate.to_dict(), "reviewer_role": role,
            "native_review": {"task_id": task_id, "run_id": run_id, "session_id": session, "profile": role},
            "checks": checks, "checks_identity": _identity(checks), "verdict": verdict,
            "criterion_evidence": criterion, "findings": findings or []}


def _install_board(ctl, store, board, cards):
    """Synthetic transport only: all public coordinator effects remain real store effects."""
    anchor_reader = board.read_task
    links = {task_id: {"parents": [], "children": [], "events": []} for task_id in cards}
    runs = {}
    sequence = {"value": 0}

    def create(action, *, title, body, assignee, workspace, idempotency_key):
        # Match HermesBoardAdapter: the durable claim is the adapter's immediate
        # pre-create protocol, not a coordinator fake-only workaround.
        claim = getattr(board, "claim_create_attempt", None)
        assert callable(claim)
        claim(action.scope, action.key)
        assert action.target["native_parent"] is False
        sequence["value"] += 1
        task_id = "native-" + action.target.get("ticket_id", action.key)
        native = {"id": task_id, "status": "blocked", "assignee": assignee, "title": title, "body": body,
                  "workspace": workspace, "workspace_path": workspace.removeprefix("dir:")}
        digest = hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest()
        cards[task_id] = BoardSnapshot(native, (), (), (), (), (), "fixture", digest)
        links[task_id] = {"parents": [], "children": [], "events": []}
        show = {"task": native, "parents": [], "children": [], "runs": [], "events": [], "comments": [], "latest_summary": None}
        readback = cards[task_id].to_dict()
        readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1", "show": show, "runs": []}
        return ActionResult(action.key, "verified", "created", readback)

    def read(task_id):
        if task_id == ctl.scope["anchor_task_id"]:
            return anchor_reader(task_id)
        card = cards[task_id]
        return BoardSnapshot(card.native_task, tuple({"id": x} for x in links[task_id]["parents"]), tuple(runs.get(task_id, ())), card.comments, card.events, card.attachments, "fixture", card.digest)

    def release(action, task_id, reason):
        card = cards[task_id]
        updated = _snapshot(card, status="ready", comments=({"body": "UNBLOCK: " + board._native_marker(action)},))
        cards[task_id] = updated
        return ActionResult(action.key, "verified", "released", read(task_id).to_dict())

    def link(action, source, child):
        links[source]["children"].append(child); links[child]["parents"].append(source)
        links[child]["events"].append({"kind": "linked", "payload": {"child": child, "parent": source}, "run_id": None, "created_at": int(time.time())})
        return ActionResult(action.key, "verified", "linked", None)

    def raw_reader(_scope, task_ids):
        result = {}
        for task_id in task_ids:
            card = cards[task_id]
            result[task_id] = {"task": dict(card.native_task), "parents": list(links[task_id]["parents"]),
                "children": list(links[task_id]["children"]), "runs": list(runs.get(task_id, ())), "events": list(links[task_id]["events"]), "comments": [],
                "latest_summary": None, "show_has_runs": True, "show_runs": list(runs.get(task_id, ()))}
        return result

    def verify(action):
        task_id = "native-" + action.target.get("ticket_id", action.key)
        if action.effect == "create_held" and task_id in cards:
            snapshot = read(task_id)
            native = dict(snapshot.native_task)
            show = {"task": native, "parents": [], "children": [], "runs": [], "events": [], "comments": [], "latest_summary": None}
            readback = snapshot.to_dict()
            readback["raw_capture_v1"] = {"kind": "hermes_kanban_raw_capture_v1", "show": show, "runs": []}
            return ActionResult(action.key, "verified", "fixture held replay", readback)
        return ActionResult(action.key, "unknown", "fixture verifier unused", None)

    def native_marker(action):
        marker_payload = json.dumps({"action_key": action.key, "anchor_task_id": action.scope["anchor_task_id"],
                                     "board_id": action.scope["board_id"], "effect": action.effect},
                                    sort_keys=True, separators=(",", ":"))
        return "<!-- local-first-native:v1:sha256:" + hashlib.sha256(marker_payload.encode()).hexdigest() + " -->"

    board.create_held = create; board.read_task = read; board.release = release; board.link = link
    board._native_marker = native_marker
    board.verify_effect = verify
    board.read_accepted_active_tranche_raw_cards = raw_reader
    board.read_scoped_run = lambda _scope, task_id, run_id: next(dict(run) for run in runs[task_id] if str(run["id"]) == str(run_id))
    return runs


def _candidate(adapter, repo, ticket_id, base, filename):
    attempt = adapter.create_attempt(ticket_id, 1, base)
    (attempt.path / filename).write_text(ticket_id + "\n")
    _git(attempt.path, "add", filename); _git(attempt.path, "commit", "-qm", ticket_id)
    head = _git(attempt.path, "rev-parse", "HEAD")
    return CandidateIdentity("fixture-repo", str(attempt.path), base, head, "content-" + ticket_id, "diff-" + ticket_id, "implementation-" + ticket_id, "contract-" + ticket_id)


def _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, ticket_id, task_id, candidate, review):
    """Synthesize the public native handoff/claim history, not a caller assertion."""
    implementation, reviewer = ctl._local_review_roles()
    implementation_session = "implementation-session-" + ticket_id
    reviewer_session = review["native_review"]["session_id"]
    runs[task_id] = ({"id": candidate.originating_run_id, "task_id": task_id, "profile": implementation,
                      "status": "running", "metadata": {"worker_session_id": implementation_session}},)
    cards[task_id] = _snapshot(cards[task_id], status="running", assignee=implementation)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", candidate.originating_run_id)
    monkeypatch.setenv("HERMES_SESSION_ID", implementation_session)
    ctl.git_observer = lambda _scope: {"candidate": candidate.to_dict(), "checks": review["checks"],
                                       "checks_identity": review["checks_identity"], "criterion_ids": ["AC-1"]}
    key = "local-handoff:" + ticket_id
    assert ctl.request_local_review(task_id, candidate, implementation_profile=implementation,
                                    reviewer_profile=reviewer, summary="fixture candidate", operation_key=key)["outcome"] == "proposed"
    marker = next(op.target["review_marker"] for op in store.read_scope(ctl.scope)["operations"] if op.key == key)
    runs[task_id] = (
        {"id": candidate.originating_run_id, "task_id": task_id, "profile": implementation,
         "status": "completed", "outcome": "review_requested",
         "metadata": {"local_first_review": marker, "worker_session_id": implementation_session}},
        {"id": review["native_review"]["run_id"], "task_id": task_id, "profile": reviewer,
         "status": "completed", "outcome": "completed", "metadata": {"worker_session_id": reviewer_session}},
    )
    cards[task_id] = _snapshot(cards[task_id], status="done", assignee=reviewer, events=(
        {"kind": "review_requested", "run_id": candidate.originating_run_id,
         "payload": {"implementer": implementation, "reviewer": reviewer}},
        {"kind": "claimed", "run_id": review["native_review"]["run_id"],
         "payload": {"run_id": review["native_review"]["run_id"], "source_status": "review"}},
        {"kind": "completed", "run_id": review["native_review"]["run_id"], "payload": {"summary": "approved"}},
    ))


def _no_effect_state(store, cards, git, tranche_id, base):
    return (tuple(store.connection.iterdump()), dict(cards),
            git.existing_execution_base(tranche_id, base))


def _exercise_public_m4_core_flow(tmp_path, monkeypatch, *, public_driver_factory=None, public_setup_factory=None,
                                   before_accept=None):
    """Run the seeded regression or a fresh public plan lifecycle setup."""
    repo = tmp_path / "repo"; repo.mkdir(); _git(repo, "init", "-q"); _git(repo, "config", "user.name", "M4"); _git(repo, "config", "user.email", "m4@example.invalid")
    (repo / "base.txt").write_text("base\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD"); git = GitWorktreeAdapter(repo, tmp_path / "attempts"); git.resolve_execution_base("TR-A", base)
    if public_setup_factory is None:
        ctl, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
        runs = _install_board(ctl, store, board, cards)
        public = None if public_driver_factory is None else public_driver_factory(ctl, store, board, cards, repo, git)
    else:
        ctl, store, board, cards, runs, public = public_setup_factory(repo, git, base)
    try:
        if public_setup_factory is None:
            ctl.configured_roles = {"implementation_profile": "implementer", "local_review_profile": "local", "paid_review_profile": "paid", "planning_profile": "default"}
        ctl.budget_policy = BudgetPolicy(5, 5, 5, 5, 5)
        if public is None:
            held = ctl.prepare_active_tranche("plan-1")
            linked = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        else:
            pieces = [public.prepare_piece("plan-1", ticket_id) for ticket_id in ("TK-A", "TK-B")]
            held = {"outcome": "held", "completed": len(pieces), "pieces": pieces}
            linked = public.link_piece("plan-1", "TK-B", "TK-A")
        assert held["outcome"] == "held" and held["completed"] == 2
        assert linked["outcome"] == "linked"

        # An arbitrary unaccepted ticket cannot reserve a release or alter native state.
        before = _no_effect_state(store, cards, git, "TR-A", base)
        rejected = ctl.release_active_piece("plan-1", "NOT-ACCEPTED")
        assert rejected["outcome"] == "held"
        assert _no_effect_state(store, cards, git, "TR-A", base) == before

        # The declared child remains a valid held card after link materialization.
        assert (ctl.release_active_piece("plan-1", "TK-A") if public is None else public.release_piece("plan-1", "TK-A"))["outcome"] == "released"
        assert (ctl.release_active_piece("plan-1", "TK-B") if public is None else public.release_piece("plan-1", "TK-B"))["outcome"] == "released"
        if public is None:
            accepted_reader = store.read_accepted_plan
            store.read_accepted_plan = lambda scope, plan_id: {**accepted_reader(scope, plan_id), "base_sha": base}
        ctl.combined_check_runner = lambda context: {"head_sha": context["head_sha"], "checks": [{"check_id": "smoke", "command": "python -c pass", "exit_code": 0, "output_sha256": "sha256:" + context["head_sha"]}]}

        first = _candidate(git, repo, "TK-A", base, "a.txt")
        task_a = next(item["task_id"] for item in held["pieces"] if item["ticket_id"] == "TK-A")
        local_a = _review("local-A-review", first, role="local", task_id=task_a, run_id="local-A", session="local-A-session")
        # A completed-looking reviewer run without a worker-owned handoff is a
        # direct caller bypass and must leave the full store untouched.
        runs[task_a] = ({"id": "local-A", "task_id": task_a, "profile": "local", "status": "completed", "metadata": {"worker_session_id": "local-A-session"}},)
        cards[task_a] = _snapshot(cards[task_a], status="done", assignee="local", events=(
            {"kind": "claimed", "run_id": "local-A", "payload": {"source_status": "review"}},
            {"kind": "completed", "run_id": "local-A", "payload": {"summary": "approved"}},
        ))
        before = tuple(store.connection.iterdump())
        assert ctl.submit_local_review("plan-1", "TK-A", first, local_a)["outcome"] == "held"
        assert tuple(store.connection.iterdump()) == before
        _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, "TK-A", task_a, first, local_a)
        submitted_a = ctl.submit_local_review("plan-1", "TK-A", first, local_a) if public is None else public.submit_local_review(task_a, first, local_a)
        assert submitted_a["outcome"] in ({"approved"} if public is None else {"accepted"}), submitted_a
        assert (ctl.integrate_active_piece("plan-1", "TK-A", first, review_id="local-A-review", git_adapter=git) if public is None else public.integrate_piece("plan-1", "TK-A", "local-A-review"))["outcome"] == "integrated"

        # A partial serial chain cannot run checks or create a paid-review card.
        before = _no_effect_state(store, cards, git, "TR-A", base)
        incomplete = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert incomplete["outcome"] == "held"
        assert _no_effect_state(store, cards, git, "TR-A", base) == before

        second = _candidate(git, repo, "TK-B", first.head_sha, "b.txt")
        task_b = next(item["task_id"] for item in held["pieces"] if item["ticket_id"] == "TK-B")
        local_b = _review("local-B-review", second, role="local", task_id=task_b, run_id="local-B", session="local-B-session")
        _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, "TK-B", task_b, second, local_b)
        assert (ctl.submit_local_review("plan-1", "TK-B", second, local_b) if public is None else public.submit_local_review(task_b, second, local_b))["outcome"] in ({"approved"} if public is None else {"accepted"})
        assert (ctl.integrate_active_piece("plan-1", "TK-B", second, review_id="local-B-review", git_adapter=git) if public is None else public.integrate_piece("plan-1", "TK-B", "local-B-review"))["outcome"] == "integrated"

        request = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git) if public is None else public.prepare_paid_review("plan-1")
        assert request["outcome"] == "held"
        paid_create = next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == request["review_request_key"])
        assert paid_create.target["native_parent"] is False
        paid_release = ctl.release_paid_integrated_review("plan-1", git_adapter=git) if public is None else public.release_paid_review("plan-1")
        assert paid_release["outcome"] == "released"
        paid_task = request["review_task_id"]
        cards[paid_task] = _snapshot(cards[paid_task], status="done", assignee="paid")
        runs[paid_task] = ({"id": "paid-1", "task_id": paid_task, "profile": "paid", "status": "completed", "metadata": {"worker_session_id": "paid-session"}},)
        candidate = next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == request["review_request_key"]).readback["candidate"]
        paid = _review("paid-changes", CandidateIdentity.from_dict(candidate), role="paid", task_id=paid_task, run_id="paid-1", session="paid-session", verdict="changes_requested", findings=[{"finding_id":"finding-1","criterion_id":"AC-1","severity":"major","summary":"fixture"}])
        # changes requested is owned by a running reviewer, not terminal approval.
        runs[paid_task] = ({**runs[paid_task][0], "status": "running"},)
        assert (ctl.submit_paid_integrated_review("plan-1", paid, git_adapter=git) if public is None else public.submit_paid_review("plan-1", paid))["outcome"] == "changes_requested"
        correction = ctl.prepare_paid_correction("plan-1", "paid-changes", git_adapter=git) if public is None else public.prepare_paid_correction("plan-1", "paid-changes")
        assert correction["outcome"] == "held"
        correction_create = next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == correction["operation_key"])
        assert correction_create.target["native_parent"] is False

        # A real CAS ref advance makes the correction's paid evidence stale; release
        # stays held without consuming budget or unblocking the held correction.
        stale = _candidate(git, repo, "TK-STALE", second.head_sha, "stale.txt")
        assert git.advance_integration_head("TR-A", second.head_sha, stale.head_sha) == stale.head_sha
        before = _no_effect_state(store, cards, git, "TR-A", base)
        stale_release = ctl.release_paid_correction("plan-1", "paid-changes", git_adapter=git)
        assert stale_release["outcome"] == "held"
        assert _no_effect_state(store, cards, git, "TR-A", base) == before
        _git(repo, "update-ref", git.integration_head_ref("TR-A"), second.head_sha, stale.head_sha)
        correction_release = ctl.release_paid_correction("plan-1", "paid-changes", git_adapter=git) if public is None else public.release_paid_correction("plan-1", "paid-changes")
        assert correction_release["outcome"] == "released"

        correction_candidate = _candidate(git, repo, "TK-CORRECTION", second.head_sha, "correction.txt")
        correction_task = correction["correction_task_id"]
        correction_review = _review("local-correction-review", correction_candidate, role="local", task_id=correction_task, run_id="local-correction", session="local-correction-session")
        _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, "TK-CORRECTION", correction_task, correction_candidate, correction_review)
        assert (ctl.submit_local_review("plan-1", correction["operation_key"], correction_candidate, correction_review) if public is None else public.submit_local_review(correction_task, correction_candidate, correction_review))["outcome"] in ({"approved"} if public is None else {"accepted"})
        assert (ctl.integrate_active_piece("plan-1", correction["operation_key"], correction_candidate, review_id="local-correction-review", git_adapter=git) if public is None else public.integrate_piece("plan-1", correction["operation_key"], "local-correction-review"))["outcome"] == "integrated"
        request2 = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git) if public is None else public.prepare_paid_review("plan-1")
        assert request2["outcome"] == "held"
        assert (ctl.release_paid_integrated_review("plan-1", git_adapter=git) if public is None else public.release_paid_review("plan-1"))["outcome"] == "released"
        paid_task2 = request2["review_task_id"]
        cards[paid_task2] = _snapshot(cards[paid_task2], status="done", assignee="paid")
        runs[paid_task2] = ({"id":"paid-2", "task_id":paid_task2, "profile":"paid", "status":"completed", "metadata":{"worker_session_id":"paid-2-session"}},)
        paid_candidate2 = next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == request2["review_request_key"]).readback["candidate"]
        approved = _review("paid-approved", CandidateIdentity.from_dict(paid_candidate2), role="paid", task_id=paid_task2, run_id="paid-2", session="paid-2-session")
        assert (ctl.submit_paid_integrated_review("plan-1", approved, git_adapter=git) if public is None else public.submit_paid_review("plan-1", approved))["outcome"] == "approved"
        if before_accept is not None and before_accept(ctl, store, board, cards, runs, git, public, "plan-1", "paid-approved") is False:
            return
        accepted = ctl.accept_tranche("plan-1", "paid-approved", git_adapter=git, authorize_successor=True) if public is None else public.accept_tranche("plan-1", "paid-approved")
        assert accepted["outcome"] == "accepted"
        assert git.existing_execution_base("TR-A", base) == correction_candidate.head_sha

        # The successor needs its own accepted contract, rooted at this exact
        # real integration head, before public held materialization/release.
        parent_reader = store.read_accepted_plan
        parent_plan_reader = store.read_plan
        parent = parent_reader(ctl.scope, "plan-1")
        request0, proposal0, _ = successor_fixture()
        successor_request = dataclasses.replace(
            request0, board_id=ctl.scope["board_id"], anchor_id=ctl.scope["anchor_task_id"],
            base_sha=correction_candidate.head_sha,
            repository_identity=parent["repository_identity"],
            root_contract_hash=parent["root_contract_hash"],
        )
        first_tranche, later_tranche = proposal0.plan.tranches
        successor_first = dataclasses.replace(
            first_tranche,
            tickets=tuple(dataclasses.replace(ticket, ticket_id="SK-" + ticket.ticket_id.removeprefix("TK-"))
                          for ticket in first_tranche.tickets),
        )
        successor_later = dataclasses.replace(
            later_tranche,
            tickets=tuple(dataclasses.replace(ticket, ticket_id="SK-" + ticket.ticket_id.removeprefix("TK-"),
                                              dependencies=("SK-A",))
                          for ticket in later_tranche.tickets),
        )
        successor_plan = dataclasses.replace(
            proposal0.plan, plan_id="successor-plan", tranches=(successor_first, successor_later),
            criterion_coverage={"AC-1": ("SK-A", "SK-B"), "AC-2": ("SK-C",)},
        )
        successor_proposal = dataclasses.replace(
            proposal0, request_identity=successor_request.identity, plan=successor_plan,
        )
        successor_evidence = evidence_payload(
            successor_request, successor_proposal, planner_task_id="planner", planner_run_id="run",
            planner_session_id="session", planner_profile="paid-planner",
        )
        successor = {
            "schema_version": 1, "plan_id": "successor-plan", "request_identity": successor_request.identity,
            "proposal_hash": successor_proposal.proposal_hash, "plan_contract_hash": successor_plan.contract_hash,
            "planner": successor_evidence["planner"], "repository_identity": parent["repository_identity"],
            "base_sha": correction_candidate.head_sha, "snapshot_hash": successor_request.snapshot_hash,
            "root_contract_hash": parent["root_contract_hash"],
            "active_tranche": {"tranche_id": "TR-A", "ordinal": 0}, "route": dict(parent["route"]),
        }
        successor["acceptance_identity"] = "sha256:" + hashlib.sha256(
            json.dumps(successor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        store.read_accepted_plan = lambda scope, plan_id: parent if plan_id == "plan-1" else successor
        store.read_plan = lambda scope, plan_id: parent_plan_reader(scope, plan_id) if plan_id == "plan-1" else successor_evidence
        store.read_planning_request = lambda scope, request_id=None: {"request": request_payload(successor_request)}
        ctl.planning_observer = lambda scope: {"request": request_payload(successor_request)}
        materialized = ctl.materialize_accepted_successor(
            "plan-1", "successor-plan", accepted["acceptance_operation_key"], git_adapter=git,
        )
        assert materialized["outcome"] == "held" and materialized["materialization"]["completed"] == 2, materialized["materialization"].get("pieces")
        successor_ticket = materialized["materialization"]["pieces"][0]["ticket_id"]
        successor_task = materialized["materialization"]["pieces"][0]["task_id"]
        assert cards[successor_task].native_task["status"] == "blocked"
        assert not [event for event in store.read_scope(ctl.scope)["budget_events"]
                    if event["event_id"].startswith("implementation_attempts:") and event["source_task_id"] == successor_task]
        assert not [op for op in store.read_scope(ctl.scope)["operations"]
                    if op.effect == "release" and op.target.get("task_id") == successor_task]
        successor_release = ctl.release_accepted_successor_piece(
            "plan-1", "successor-plan", accepted["acceptance_operation_key"], successor_ticket, git_adapter=git,
        )
        assert successor_release["outcome"] == "released" and successor_release["actions_attempted"] == 1
        effects = {op.effect for op in store.read_scope(ctl.scope)["operations"]}
        assert {"create_held", "link", "release", "git_integrate", "combined_checks", "tranche_accept"} <= effects
    finally:
        store.close()


def test_public_m4_core_flow_uses_actual_effect_journals_and_real_git(tmp_path, monkeypatch):
    _exercise_public_m4_core_flow(tmp_path, monkeypatch)


def _regression_core(tmp_path, monkeypatch):
    """Small public-core setup shared by the focused M4 behavior regressions."""
    ctl, store, board, cards = _batch_fixture(tmp_path, monkeypatch)
    ctl.configured_roles = {"implementation_profile": "implementer", "local_review_profile": "local",
                            "paid_review_profile": "paid", "planning_profile": "default"}
    ctl.budget_policy = BudgetPolicy(5, 5, 5, 5, 5)
    return ctl, store, board, cards, _install_board(ctl, store, board, cards)


def test_regression_held_materialization_does_not_auto_release(tmp_path, monkeypatch):
    ctl, store, _board, cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        held = ctl.prepare_active_tranche("plan-1")
        assert held["outcome"] == "held" and held["completed"] == 2
        assert {cards[piece["task_id"]].native_task["status"] for piece in held["pieces"]} == {"blocked"}
        assert not [op for op in store.read_scope(ctl.scope)["operations"]
                    if op.effect == "release" and op.target.get("ticket_id") in {"TK-A", "TK-B"}]
    finally:
        store.close()


def test_regression_declared_serial_link_is_applied_once_and_replays(tmp_path, monkeypatch):
    ctl, store, _board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        ctl.prepare_active_tranche("plan-1")
        first = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        before = tuple(store.connection.iterdump())
        replay = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        links = [op for op in store.read_scope(ctl.scope)["operations"] if op.effect == "link"]
        assert first["outcome"] == replay["outcome"] == "linked"
        assert len(links) == 1 and links[0].target["source_ticket_id"] == "TK-A"
        assert tuple(store.connection.iterdump()) == before
    finally:
        store.close()


@pytest.mark.parametrize("cancel", (False, True), ids=("pause", "cancel"))
def test_regression_prepared_link_rechecks_operator_intent_before_send(tmp_path, monkeypatch, cancel):
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        ctl.prepare_active_tranche("plan-1")
        prepared = ctl.prepare_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        sends = []
        real_link = board.link
        board.link = lambda *args: (sends.append(args[0].key) or real_link(*args))
        store.set_operator_intent(PauseIntent(ctl.scope, "operator", 1, cancel, cancel))
        result = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        operation = next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == prepared["operation_key"])
        assert result["outcome"] == "held" and result["actions_attempted"] == 0
        assert operation.phase == "pending" and sends == []
    finally:
        store.close()


def test_m5_reopen_after_v2_link_presend_before_claim_sends_once(tmp_path, monkeypatch):
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        ctl.prepare_active_tranche("plan-1")
        native_calls = []
        real_link, real_begin = board.link, store.begin_effect_attempt
        board.link = lambda action, source, target: (native_calls.append(action.key) or real_link(action, source, target))
        store.begin_effect_attempt = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash before claim"))
        with pytest.raises(RuntimeError, match="crash before claim"):
            ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert not native_calls
        key = next(op.key for op in store.read_scope(ctl.scope)["operations"]
                   if op.effect == "link")
        assert next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == key).phase == "pending"
        store.begin_effect_attempt = real_begin
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-presend.lock"), budget_policy=ctl.budget_policy)
        assert resumed.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A") == {
            "outcome": "linked", "operation_key": key, "actions_attempted": 1,
        }
        assert native_calls == [key]
    finally:
        reopened.close()


def test_m5_reopen_after_v2_link_claim_before_native_effect_stays_partial_without_resend(tmp_path, monkeypatch):
    """A durable unknown claim with no native edge is reconciliation-only after restart."""
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    native_calls = []
    try:
        ctl.prepare_active_tranche("plan-1")
        real_begin = store.begin_effect_attempt

        def claim_then_crash(scope, key):
            real_begin(scope, key)
            raise RuntimeError("crash after durable claim before native link")

        board.link = lambda action, source, target: native_calls.append(action.key)
        store.begin_effect_attempt = claim_then_crash
        with pytest.raises(RuntimeError, match="crash after durable claim"):
            ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        key = next(op.key for op in store.read_scope(ctl.scope)["operations"] if op.effect == "link")
        assert native_calls == []
        assert next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == key).phase == "unknown"
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-claimed.lock"), budget_policy=ctl.budget_policy)
        result = resumed.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert result["outcome"] == "partial" and result["actions_attempted"] == 0
        assert native_calls == []
        assert next(op for op in reopened.read_scope(resumed.scope)["operations"] if op.key == key).phase == "unknown"
    finally:
        reopened.close()


def test_m5_reopen_after_v2_link_effect_before_receipt_checkpoint_stays_partial_without_resend(tmp_path, monkeypatch):
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        ctl.prepare_active_tranche("plan-1")
        real_link = board.link

        def link_then_crash(action, source, target):
            real_link(action, source, target)
            raise RuntimeError("crash after native effect before acknowledgement")

        board.link = link_then_crash
        first = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert first["outcome"] == "partial" and first["actions_attempted"] == 1
        key = first["operation_key"]
        assert next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == key).phase == "unknown"
    finally:
        store.close()

    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-link.lock"), budget_policy=ctl.budget_policy)
        verified = resumed.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert verified["outcome"] == "partial" and verified["actions_attempted"] == 0
        assert next(op for op in reopened.read_scope(resumed.scope)["operations"] if op.key == key).phase == "unknown"
    finally:
        reopened.close()


def test_m5_reopen_after_validated_v2_receipt_checkpoint_acks_without_resend(tmp_path, monkeypatch):
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        ctl.prepare_active_tranche("plan-1")
        real_ack = store.ack_effect
        store.ack_effect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash after receipt checkpoint"))
        with pytest.raises(RuntimeError, match="crash after receipt checkpoint"):
            ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        key = next(op.key for op in store.read_scope(ctl.scope)["operations"] if op.effect == "link")
        assert next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == key).phase == "unknown"
        assert store.read_validated_link_receipt_checkpoint(ctl.scope, key) is not None
        store.ack_effect = real_ack
    finally:
        store.close()

    native_calls = []
    board.link = lambda action, source, target: native_calls.append(action.key)
    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-receipt-checkpoint.lock"), budget_policy=ctl.budget_policy)
        assert resumed.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A") == {
            "outcome": "linked", "operation_key": key, "actions_attempted": 0,
        }
        assert native_calls == []
        assert next(op for op in reopened.read_scope(resumed.scope)["operations"] if op.key == key).phase == "applied"
    finally:
        reopened.close()


def test_m5_reopen_after_v2_link_acknowledgement_replays_without_native_effect(tmp_path, monkeypatch):
    ctl, store, board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    database = store.path
    try:
        ctl.prepare_active_tranche("plan-1")
        first = ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")
        assert first["outcome"] == "linked" and first["actions_attempted"] == 1
        key = first["operation_key"]
    finally:
        store.close()

    native_calls = []
    board.link = lambda action, source, target: native_calls.append(action.key)
    reopened = EvidenceStore.open(database)
    try:
        resumed = Coordinator(ctl.scope, board=board, store=reopened,
                              lock=instance_lock(tmp_path / "resumed-acknowledged.lock"), budget_policy=ctl.budget_policy)
        assert resumed.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A") == {
            "outcome": "linked", "operation_key": key, "actions_attempted": 0,
        }
        assert native_calls == []
    finally:
        reopened.close()


def test_regression_named_piece_release_consumes_one_budget_unit_and_replays(tmp_path, monkeypatch):
    ctl, store, _board, cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        held = ctl.prepare_active_tranche("plan-1")
        task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-A")
        assert ctl.release_active_piece("plan-1", "TK-A")["outcome"] == "released"
        replay = ctl.release_active_piece("plan-1", "TK-A")
        budget = [event for event in store.read_scope(ctl.scope)["budget_events"]
                  if event["event_id"].startswith("implementation_attempts:")]
        assert cards[task_id].native_task["status"] == "ready"
        assert replay["actions_attempted"] == 0 and len(budget) == 1
    finally:
        store.close()


def test_regression_unaccepted_piece_release_has_no_effect(tmp_path, monkeypatch):
    ctl, store, _board, cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        ctl.prepare_active_tranche("plan-1")
        before = (tuple(store.connection.iterdump()), dict(cards))
        assert ctl.release_active_piece("plan-1", "NOT-ACCEPTED")["outcome"] == "held"
        assert (tuple(store.connection.iterdump()), dict(cards)) == before
    finally:
        store.close()


def test_regression_paid_review_waits_for_complete_serial_integration(tmp_path, monkeypatch):
    ctl, store, _board, _cards, _runs = _regression_core(tmp_path, monkeypatch)
    try:
        ctl.prepare_active_tranche("plan-1")
        ctl.combined_check_runner = lambda context: {"head_sha": context["head_sha"], "checks": []}
        git = type("UnreadIntegratedGit", (), {"existing_execution_base": lambda _self, _tranche, base: base,
                                                "primary_checkout": tmp_path})()
        before = tuple(store.connection.iterdump())
        held = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert held["outcome"] == "held"
        assert tuple(store.connection.iterdump()) == before
        assert not [op for op in store.read_scope(ctl.scope)["operations"] if op.effect == "combined_checks"]
    finally:
        store.close()


def test_regression_serial_git_integration_requires_observed_local_approval(tmp_path, monkeypatch):
    ctl, store, _board, _cards, runs = _regression_core(tmp_path, monkeypatch)
    try:
        held = ctl.prepare_active_tranche("plan-1")
        assert ctl.release_active_piece("plan-1", "TK-A")["outcome"] == "released"
        repo = tmp_path / "repo"; repo.mkdir()
        _git(repo, "init", "-q"); _git(repo, "config", "user.name", "M4"); _git(repo, "config", "user.email", "m4@example.invalid")
        (repo / "base.txt").write_text("base\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
        base = _git(repo, "rev-parse", "HEAD"); git = GitWorktreeAdapter(repo, tmp_path / "attempts")
        git.resolve_execution_base("TR-A", base)
        accepted_reader = store.read_accepted_plan
        store.read_accepted_plan = lambda scope, plan_id: {**accepted_reader(scope, plan_id), "base_sha": base}
        candidate = _candidate(git, repo, "TK-A", base, "a.txt")
        task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == "TK-A")
        review = _review("local-A-review", candidate, role="local", task_id=task_id, run_id="local-A", session="local-A-session")
        assert ctl.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=git)["outcome"] == "held"
        _complete_same_card_local_review(ctl, store, _cards, runs, monkeypatch, "TK-A", task_id, candidate, review)
        assert ctl.submit_local_review("plan-1", "TK-A", candidate, review)["outcome"] == "approved"
        integrated = ctl.integrate_active_piece("plan-1", "TK-A", candidate, review_id="local-A-review", git_adapter=git)
        assert integrated["outcome"] == "integrated"
        assert git.existing_execution_base("TR-A", base) == candidate.head_sha
    finally:
        store.close()


def _complete_integrated_serial_core(tmp_path, monkeypatch):
    """Drive both accepted pieces through the public release/review/CAS APIs."""
    ctl, store, board, cards, runs = _regression_core(tmp_path, monkeypatch)
    held = ctl.prepare_active_tranche("plan-1")
    assert ctl.execute_accepted_dependency_link("plan-1", "TK-B", "TK-A")["outcome"] == "linked"
    assert ctl.release_active_piece("plan-1", "TK-A")["outcome"] == "released"
    assert ctl.release_active_piece("plan-1", "TK-B")["outcome"] == "released"
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q"); _git(repo, "config", "user.name", "M4"); _git(repo, "config", "user.email", "m4@example.invalid")
    (repo / "base.txt").write_text("base\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD"); git = GitWorktreeAdapter(repo, tmp_path / "attempts")
    git.resolve_execution_base("TR-A", base)
    accepted_reader = store.read_accepted_plan
    store.read_accepted_plan = lambda scope, plan_id: {**accepted_reader(scope, plan_id), "base_sha": base}
    first = _candidate(git, repo, "TK-A", base, "a.txt")
    second = _candidate(git, repo, "TK-B", first.head_sha, "b.txt")
    for ticket_id, candidate, review_id, run_id in (("TK-A", first, "local-A-review", "local-A"),
                                                     ("TK-B", second, "local-B-review", "local-B")):
        task_id = next(piece["task_id"] for piece in held["pieces"] if piece["ticket_id"] == ticket_id)
        review = _review(review_id, candidate, role="local", task_id=task_id, run_id=run_id, session=run_id + "-session")
        _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, ticket_id, task_id, candidate, review)
        assert ctl.submit_local_review("plan-1", ticket_id, candidate, review)["outcome"] == "approved"
        assert ctl.integrate_active_piece("plan-1", ticket_id, candidate, review_id=review_id, git_adapter=git)["outcome"] == "integrated"
    return ctl, store, board, cards, runs, git, repo, base, second


def test_regression_combined_checks_failure_blocks_paid_review_and_acceptance(tmp_path, monkeypatch):
    ctl, store, _board, _cards, _runs, git, _repo, base, head = _complete_integrated_serial_core(tmp_path, monkeypatch)
    try:
        calls = []
        ctl.combined_check_runner = lambda context: (calls.append(dict(context)) or {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "smoke", "command": "false", "exit_code": 1,
                        "output_sha256": "sha256:" + context["head_sha"]}],
        })
        blocked = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert blocked == {"outcome": "held", "reason": "combined_checks_failed_or_noncanonical"}
        assert calls == [{"plan_id": "plan-1", "tranche_id": "TR-A", "base_sha": base, "head_sha": head.head_sha}]
        effects = {op.effect for op in store.read_scope(ctl.scope)["operations"]}
        assert "combined_checks" not in effects and "tranche_accept" not in effects
        assert not [op for op in store.read_scope(ctl.scope)["operations"]
                    if op.effect == "create_held" and op.target.get("kind") == "paid_integrated_review_v1"]
        assert ctl.accept_tranche("plan-1", "not-a-review", git_adapter=git)["outcome"] == "held"
    finally:
        store.close()


def test_regression_changed_correction_head_requires_fresh_checks_request_and_approval(tmp_path, monkeypatch):
    ctl, store, _board, cards, runs, git, repo, _base, old_head = _complete_integrated_serial_core(tmp_path, monkeypatch)
    try:
        check_heads = []
        ctl.combined_check_runner = lambda context: (check_heads.append(context["head_sha"]) or {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "smoke", "command": "python -c pass", "exit_code": 0,
                        "output_sha256": "sha256:" + context["head_sha"]}],
        })
        first_request = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert ctl.release_paid_integrated_review("plan-1", git_adapter=git)["outcome"] == "released"
        paid_task = first_request["review_task_id"]
        cards[paid_task] = _snapshot(cards[paid_task], status="done", assignee="paid")
        runs[paid_task] = ({"id": "paid-old", "task_id": paid_task, "profile": "paid", "status": "completed", "metadata": {"worker_session_id": "paid-old-session"}},)
        old_candidate = CandidateIdentity.from_dict(next(op for op in store.read_scope(ctl.scope)["operations"] if op.key == first_request["review_request_key"]).readback["candidate"])
        old_approval = _review("paid-old-approved", old_candidate, role="paid", task_id=paid_task, run_id="paid-old", session="paid-old-session")
        assert ctl.submit_paid_integrated_review("plan-1", old_approval, git_adapter=git)["outcome"] == "approved"
        cards[paid_task] = _snapshot(cards[paid_task], status="blocked", assignee="paid")
        runs[paid_task] = ({**runs[paid_task][0], "status": "running"},)
        changes = _review("paid-old-changes", old_candidate, role="paid", task_id=paid_task, run_id="paid-old", session="paid-old-session", verdict="changes_requested", findings=[{"finding_id": "f-1", "criterion_id": "AC-1", "severity": "major", "summary": "fixture"}])
        assert ctl.submit_paid_integrated_review("plan-1", changes, git_adapter=git)["outcome"] == "changes_requested"
        correction = ctl.prepare_paid_correction("plan-1", "paid-old-changes", git_adapter=git)
        assert correction["outcome"] == "held"
        assert ctl.release_paid_correction("plan-1", "paid-old-changes", git_adapter=git)["outcome"] == "released"
        candidate = _candidate(git, repo, "TK-CORRECTION", old_head.head_sha, "correction.txt")
        task_id = correction["correction_task_id"]
        local = _review("local-correction", candidate, role="local", task_id=task_id, run_id="local-correction", session="local-correction-session")
        _complete_same_card_local_review(ctl, store, cards, runs, monkeypatch, "TK-CORRECTION", task_id, candidate, local)
        assert ctl.submit_local_review("plan-1", correction["operation_key"], candidate, local)["outcome"] == "approved"
        assert ctl.integrate_active_piece("plan-1", correction["operation_key"], candidate, review_id="local-correction", git_adapter=git)["outcome"] == "integrated"
        # The actual integration ref is now the correction head.  Recovery is
        # given only a composition-root Git adapter and observer; it must not
        # recycle the old paid approval and instead creates a held request for
        # this exact current revision.
        ctl.recovery_git_adapter = git
        ctl.git_observer = lambda _scope: {"plan_id": "plan-1", "candidate": candidate.to_dict(),
                                           "checks": [{"check_id": "smoke", "outcome": "passed", "evidence": "fixture"}],
                                           "criterion_ids": ["AC-1"]}
        continued = ctl._continue_current_revision_paid_review(RecoveryIssue(
            "stale_approval", ctl.scope, paid_task, "paid-old", "stale-paid", 0,
            old_candidate.to_dict(), {"mismatched_identity_fields": ("head_sha",)}, "fixture-stale-head",
        ))
        assert continued is not None and continued["outcome"] == "continued_current_revision"
        second_request = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert second_request["outcome"] == "held" and second_request["review_request_key"] != first_request["review_request_key"]
        assert check_heads == [old_head.head_sha, candidate.head_sha]
        assert ctl.accept_tranche("plan-1", "paid-old-approved", git_adapter=git) == {"outcome": "held", "reason": "exact_current_paid_approval_required"}
    finally:
        store.close()


def test_m5_two_fresh_coordinators_share_one_held_paid_correction_generation_and_budget(tmp_path, monkeypatch):
    """Equivalent paid findings converge before a second held-card create can occur."""
    ctl, store, board, cards, runs, git, _repo, _base, head = _complete_integrated_serial_core(tmp_path, monkeypatch)
    try:
        ctl.combined_check_runner = lambda context: {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "smoke", "command": "python -c pass", "exit_code": 0,
                        "output_sha256": "sha256:" + context["head_sha"]}],
        }
        request = ctl.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert ctl.release_paid_integrated_review("plan-1", git_adapter=git)["outcome"] == "released"
        paid_task = request["review_task_id"]
        cards[paid_task] = _snapshot(cards[paid_task], status="done", assignee="paid")
        runs[paid_task] = ({"id": "paid-correction", "task_id": paid_task, "profile": "paid",
                            "status": "completed", "metadata": {"worker_session_id": "paid-correction-session"}},)
        candidate = CandidateIdentity.from_dict(next(op for op in store.read_scope(ctl.scope)["operations"]
                                                     if op.key == request["review_request_key"]).readback["candidate"])
        approved = _review("paid-correction-approved", candidate, role="paid", task_id=paid_task,
                           run_id="paid-correction", session="paid-correction-session")
        assert ctl.submit_paid_integrated_review("plan-1", approved, git_adapter=git)["outcome"] == "approved"
        cards[paid_task] = _snapshot(cards[paid_task], status="blocked", assignee="paid")
        runs[paid_task] = ({**runs[paid_task][0], "status": "running"},)
        changes = _review("paid-correction-findings", candidate, role="paid", task_id=paid_task,
                          run_id="paid-correction", session="paid-correction-session",
                          verdict="changes_requested", findings=[{"finding_id": "f-concurrent", "criterion_id": "AC-1",
                                                                    "severity": "major", "summary": "fixture"}])
        assert ctl.submit_paid_integrated_review("plan-1", changes, git_adapter=git)["outcome"] == "changes_requested"

        first = ctl.prepare_paid_correction("plan-1", "paid-correction-findings", git_adapter=git)
        other = Coordinator(ctl.scope, board=board, store=store,
                            lock=instance_lock(tmp_path / "other-correction.lock"), budget_policy=ctl.budget_policy)
        other.configured_roles = dict(ctl.configured_roles)
        second = other.prepare_paid_correction("plan-1", "paid-correction-findings", git_adapter=git)
        corrections = [op for op in store.read_scope(ctl.scope)["operations"]
                       if op.effect == "create_held" and op.target.get("kind") == "paid_correction_v1"]
        charges = [event for event in store.read_scope(ctl.scope)["budget_events"]
                   if event["event_id"].startswith("review_corrections:")]
        assert first["outcome"] == second["outcome"] == "held"
        assert first["actions_attempted"] == 1 and second["actions_attempted"] == 0
        assert first["operation_key"] == second["operation_key"]
        assert len(corrections) == len(charges) == 1
        assert corrections[0].readback["head_sha"] == head.head_sha
    finally:
        store.close()


def test_regression_accepted_parent_successor_stays_held_until_named_release(tmp_path, monkeypatch):
    """Exercise the real accepted-parent/successor path with its held-state fence."""
    _exercise_public_m4_core_flow(tmp_path, monkeypatch)
