"""Typed planner decisions retain the canonical v1 evidence contract."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from local_first_orchestrator.decomposition_planner import (
    PlannerError, PlanningRequest, parse_proposal, planner_decisions_schema,
    planner_schema, proposal_from_decisions, serialize_proposal,
)
from local_first_orchestrator import plugin_tools
from tests.test_m4_plan_evidence import proposal, request


def _decisions(req: PlanningRequest) -> dict:
    return json.loads(serialize_proposal(proposal(req)))["plan"]


def test_decision_and_envelope_schemas_share_the_valid_canonical_plan():
    req = request()
    document = json.loads(serialize_proposal(proposal(req)))
    decisions = document["plan"]
    for schema in (planner_schema(), planner_schema(req), planner_decisions_schema(), planner_decisions_schema(req)):
        Draft202012Validator.check_schema(schema)
    assert not list(Draft202012Validator(planner_schema(req)).iter_errors(document))
    assert not list(Draft202012Validator(planner_decisions_schema(req)).iter_errors(decisions))
    assert parse_proposal(json.dumps(document, sort_keys=True, separators=(",", ":")), req) == proposal(req)


def test_typed_decisions_construct_the_same_v1_proposal_and_hash():
    req = request()
    expected = parse_proposal(serialize_proposal(proposal(req)), req)
    actual = proposal_from_decisions(_decisions(req), req)
    assert actual == expected
    assert actual.proposal_hash == expected.proposal_hash
    assert set(json.loads(serialize_proposal(actual))) == {"schema_version", "request_identity", "execution_order", "plan"}


@pytest.mark.parametrize("mutation", ("unknown", "budget", "outer"))
def test_typed_decisions_reject_bad_or_model_owned_fields(mutation):
    req = request()
    decisions = _decisions(req)
    if mutation == "unknown":
        decisions["tranches"][0]["tickets"][0]["patch_budget"]["unknown"] = 1
    elif mutation == "budget":
        decisions["tranches"][0]["tickets"][0]["patch_budget"]["max_attempts"] = req.max_attempts + 1
    else:
        decisions["request_identity"] = req.identity
    assert list(Draft202012Validator(planner_decisions_schema(req)).iter_errors(decisions))
    with pytest.raises(PlannerError):
        proposal_from_decisions(decisions, req)


def test_empty_non_goals_never_emits_empty_allof():
    base = request()
    req = PlanningRequest(**{**base.__dict__, "non_goals": ()})
    schema = planner_decisions_schema(req)
    Draft202012Validator.check_schema(schema)
    assert "allOf" not in schema["properties"]["tranches"]["items"]
    assert "allOf" not in schema["properties"]["tranches"]["items"]["properties"]["tickets"]["items"]


def test_public_schema_breaks_legacy_selector_before_runtime_construction():
    calls = []
    registered = {}
    class Context:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs
    plugin_tools.register_tools(Context(), runtime_factory=lambda scope: calls.append(scope))
    schema = registered["local_first_submit_plan"]["schema"]
    assert "decisions" in schema["parameters"]["properties"]
    assert "proposal_json" not in schema["parameters"]["properties"]
    result = json.loads(registered["local_first_submit_plan"]["handler"](
        {"board_id": "b", "anchor_task_id": "a", "proposal_json": "{}"}))
    assert result["ok"] is False
    assert "unsupported selectors" in result["error"]
    assert calls == []
