"""Pure plan evidence serialization and reconstruction; no lifecycle effects."""
from __future__ import annotations
import json
from typing import Any, Mapping
from .decomposition_planner import PlanningRequest, PlanProposal, parse_proposal, request_payload, serialize_proposal

def request_from_payload(value: Mapping[str, Any]) -> PlanningRequest:
    if not isinstance(value, Mapping) or set(value) != set(PlanningRequest.__dataclass_fields__): raise ValueError("planning request fields mismatch")
    data = dict(value)
    for key in ("expected_criteria", "authorized_paths"):
        items = data[key]
        if type(items) is not list or any(type(item) is not str for item in items) or len(items) != len(set(items)):
            raise ValueError(f"{key} must contain unique strings")
        data[key] = frozenset(items)
    commands = data["verification_commands"]
    if type(commands) is not list or not commands or any(type(command) is not list or not command or any(type(arg) is not str for arg in command) for command in commands):
        raise ValueError("verification_commands must be nested arrays of strings")
    data["verification_commands"] = tuple(tuple(command) for command in commands)
    if type(data["non_goals"]) is not list or any(type(item) is not str for item in data["non_goals"]):
        raise ValueError("non_goals must be a string array")
    data["non_goals"] = tuple(data["non_goals"])
    statements = data["criterion_statements"]
    if type(statements) is not list or any(type(pair) is not list or len(pair) != 2 or any(type(item) is not str for item in pair) for pair in statements):
        raise ValueError("criterion_statements must be pairs of strings")
    data["criterion_statements"] = tuple(tuple(pair) for pair in statements)
    try:
        request = PlanningRequest(**data)
        if request_payload(request) != dict(value):
            raise ValueError("planning request is not canonical")
        return request
    except (TypeError, UnicodeError) as error:
        raise ValueError("planning request is malformed") from error

def evidence_payload(request: PlanningRequest, proposal: PlanProposal, *, planner_task_id: str, planner_run_id: str, planner_session_id: str, planner_profile: str) -> dict[str, Any]:
    if type(request) is not PlanningRequest or type(proposal) is not PlanProposal or proposal.request_identity != request.identity: raise ValueError("validated matching request and proposal required")
    provenance = (planner_task_id, planner_run_id, planner_session_id, planner_profile)
    if any(type(x) is not str or not x for x in provenance): raise ValueError("complete native planner provenance required")
    serialized = serialize_proposal(proposal)
    if parse_proposal(serialized, request) != proposal: raise ValueError("proposal failed canonical roundtrip")
    return {"schema_version": 1, "request": request_payload(request), "request_identity": request.identity, "proposal": json.loads(serialized), "proposal_hash": proposal.proposal_hash, "planner": {"task_id": planner_task_id, "run_id": planner_run_id, "session_id": planner_session_id, "profile": planner_profile}}

def reconstruct_evidence(payload: Mapping[str, Any]) -> tuple[PlanningRequest, PlanProposal]:
    if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "request", "request_identity", "proposal", "proposal_hash", "planner"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1: raise ValueError("plan evidence shape/version invalid")
    request = request_from_payload(payload["request"])
    if payload["request_identity"] != request.identity: raise ValueError("stored request identity mismatch")
    raw = json.dumps(payload["proposal"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    proposal = parse_proposal(raw, request)
    if payload["proposal_hash"] != proposal.proposal_hash: raise ValueError("stored proposal hash mismatch")
    p = payload["planner"]
    if not isinstance(p, Mapping) or set(p) != {"task_id", "run_id", "session_id", "profile"} or any(type(v) is not str or not v for v in p.values()): raise ValueError("stored planner provenance invalid")
    return request, proposal
