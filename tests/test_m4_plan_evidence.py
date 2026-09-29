import dataclasses
import json
import sqlite3

import pytest

from local_first_orchestrator.contracts import ConflictError
from local_first_orchestrator.decomposition_planner import PlannerError
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.planning_coordinator import evidence_payload, reconstruct_evidence
from local_first_orchestrator.decomposition import DecompositionPlan, TranchePlan
from local_first_orchestrator.decomposition_planner import PlanningRequest, PlanProposal, TrancheSemantics
from local_first_orchestrator.ticket import PatchBudget, TicketContract, VerificationProfile


def request():
    return PlanningRequest(board_id="board-A", anchor_id="anchor-A", repository_identity="repo-A", base_sha="a"*40, snapshot_hash="b"*64, root_contract_hash="c"*64, expected_criteria=frozenset({"AC-1", "AC-2"}), authorized_paths=frozenset({"src/a.py", "tests/a.py"}), max_tranches=2, max_tickets=4, max_context_tokens=4096, max_patch_files=2, max_patch_lines=100, max_attempts=2, verification_commands=(("python", "-m", "pytest"),), verification_timeout_seconds=60, verification_output_limit=20000, max_payload_bytes=65536, max_json_depth=16, objective="Deliver bounded change", non_goals=("No unrelated changes",), criterion_statements=(("AC-1", "First criterion"), ("AC-2", "Second criterion")))


def proposal(req):
    verification = VerificationProfile((("python", "-m", "pytest"),), 60, 20000)
    budget = PatchBudget(2, 100, 2)
    a = TicketContract("TK-A", "Implement bounded behavior", ("AC-1",), ("No unrelated changes",), ("src/a.py",), verification, budget, 4096, ())
    b = TicketContract("TK-B", "Implement bounded behavior", ("AC-2",), ("No unrelated changes",), ("tests/a.py",), verification, budget, 4096, ("TK-A",))
    plan = DecompositionPlan("plan-1", 1, (TranchePlan("TR-A", 0, (a,), ("AC-1",)), TranchePlan("TR-B", 1, (b,), ("AC-2",))), {"AC-1": ("TK-A",), "AC-2": ("TK-B",)})
    return PlanProposal(req.identity, plan, (TrancheSemantics("TR-A", "First tranche", ("No unrelated changes",)), TrancheSemantics("TR-B", "Second tranche", ("No unrelated changes",))))

SCOPE = {"board_id": "board-A", "anchor_task_id": "anchor-A"}


def evidence():
    req = request()
    return evidence_payload(req, proposal(req), planner_task_id="planner-task", planner_run_id="native-run", planner_session_id="session", planner_profile="default")


def test_pure_evidence_roundtrip_and_validation():
    payload = evidence()
    req, plan = reconstruct_evidence(payload)
    assert req.identity == payload["request_identity"]
    assert plan.proposal_hash == payload["proposal_hash"]
    assert reconstruct_evidence(json.loads(json.dumps(payload))) == (req, plan)
    with pytest.raises((ValueError, PlannerError)):
        reconstruct_evidence({**payload, "proposal_hash": "bad"})


def test_record_and_read_plan_exact_replay_conflict_and_reopen(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = EvidenceStore.open(path, create_new=True)
    store.migrate()
    payload = evidence()
    try:
        assert store.record_plan(SCOPE, payload) == payload
        assert store.record_plan(SCOPE, payload) == payload
        plan_id = reconstruct_evidence(payload)[1].plan.plan_id
        assert store.read_plan(SCOPE, plan_id) == payload
        assert store.plan_evidence(SCOPE) == (payload,)
        for changed in (
            {**payload, "request_identity": "altered"},
            {**payload, "proposal_hash": "altered"},
            {**payload, "planner": {**payload["planner"], "run_id": "other-run"}},
            {**payload, "planner": {**payload["planner"], "task_id": "other-task"}},
            {**payload, "planner": {**payload["planner"], "session_id": "other-session"}},
            {**payload, "planner": {**payload["planner"], "profile": "other-profile"}},
        ):
            with pytest.raises((ConflictError, ValueError, PlannerError)):
                store.record_plan(SCOPE, changed)
        assert store.read_plan(SCOPE, plan_id) == payload
        with pytest.raises((ConflictError, ValueError)):
            store.record_plan({"board_id": "different", "anchor_task_id": "anchor-A"}, payload)
    finally:
        store.close()
    with EvidenceStore.open(path) as reopened:
        reopened.migrate()
        assert reopened.read_plan(SCOPE, plan_id) == payload


def test_request_payload_rejects_duplicate_arrays_wrong_types_and_string_commands():
    payload = evidence()
    req = payload["request"]
    for field, value in (("expected_criteria", ["AC-1", "AC-1"]), ("authorized_paths", ["src/a.py", "src/a.py"]), ("verification_commands", "python -m pytest"), ("non_goals", [1]), ("criterion_statements", [["AC-1", 1]])):
        bad = json.loads(json.dumps(payload))
        bad["request"][field] = value
        with pytest.raises((ValueError, PlannerError, TypeError)):
            reconstruct_evidence(bad)
