"""Compact retained planner parser boundaries after consolidation."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from local_first_orchestrator.decomposition_planner import PlannerError, parse_proposal, planner_schema, serialize_proposal
from tests.test_m4_plan_evidence import proposal, request


def test_nested_ticket_patch_budget_unknown_key_is_refused_by_schema_and_parser():
    req = request()
    document = json.loads(serialize_proposal(proposal(req)))
    document["plan"]["tranches"][0]["tickets"][0]["patch_budget"]["unknown"] = 1
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(document))
    with pytest.raises(PlannerError):
        parse_proposal(json.dumps(document, sort_keys=True, separators=(",", ":")), req)


def test_ticket_retry_budget_above_request_limit_is_refused_by_schema_and_parser():
    req = request()
    document = json.loads(serialize_proposal(proposal(req)))
    document["plan"]["tranches"][0]["tickets"][0]["patch_budget"]["max_attempts"] = req.max_attempts + 1
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(document))
    with pytest.raises(PlannerError):
        parse_proposal(json.dumps(document, sort_keys=True, separators=(",", ":")), req)
