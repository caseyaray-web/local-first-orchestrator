import dataclasses
import hashlib
import json
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from local_first_orchestrator.decomposition import DecompositionPlan, PlanValidator, TranchePlan
from local_first_orchestrator.decomposition_planner import (
    PlanningRequest, PlanProposal, PlannerError, TrancheSemantics, packet, parse_proposal,
    serialize_proposal, topological_ticket_order,
)
from local_first_orchestrator.ticket import PatchBudget, TicketContract, VerificationProfile


def request(**changes):
    args = dict(board_id="board-A", anchor_id="anchor-A", repository_identity="repo-A",
                base_sha="a" * 40, snapshot_hash="b" * 64, root_contract_hash="c" * 64,
                expected_criteria=frozenset({"AC-1", "AC-2"}), authorized_paths=frozenset({"src/a.py", "tests/a.py"}),
                max_tranches=2, max_tickets=4, max_context_tokens=4096,
                max_patch_files=2, max_patch_lines=100, max_attempts=2,
                verification_commands=(("python", "-m", "pytest"),), verification_timeout_seconds=60,
                verification_output_limit=20000, max_payload_bytes=65536, max_json_depth=16,
                objective="Deliver bounded change", non_goals=("No unrelated changes",),
                criterion_statements=(("AC-1", "First criterion"), ("AC-2", "Second criterion")))
    args.update(changes)
    return PlanningRequest(**args)


def ticket(ticket_id, criteria, paths, deps=()):
    return TicketContract(ticket_id, "Implement bounded behavior", tuple(criteria), ("No unrelated changes",),
        tuple(paths), VerificationProfile((("python", "-m", "pytest"),), 60, 20000),
        PatchBudget(2, 100, 2), 4096, tuple(deps))


def proposal(req):
    a = ticket("TK-A", ("AC-1",), ("src/a.py",))
    b = ticket("TK-B", ("AC-2",), ("tests/a.py",), ("TK-A",))
    plan = DecompositionPlan("plan-1", 1,
        (TranchePlan("TR-A", 0, (a,), ("AC-1",)), TranchePlan("TR-B", 1, (b,), ("AC-2",))),
        {"AC-1": ("TK-A",), "AC-2": ("TK-B",)})
    return PlanProposal(req.identity, plan, (TrancheSemantics("TR-A", "First tranche", ("No unrelated changes",)), TrancheSemantics("TR-B", "Second tranche", ("No unrelated changes",))))


def test_valid_request_packet_proposal_roundtrip_and_full_identity_hash():
    req = request()
    p = proposal(req)
    serialized = serialize_proposal(p)
    parsed = parse_proposal(serialized, req)
    assert parsed == p
    assert parsed.plan.contract_hash == p.plan.contract_hash
    assert packet(req) == packet(req)
    changed = dataclasses.replace(p, plan=dataclasses.replace(p.plan, plan_id="plan-2"))
    assert hashlib.sha256(serialize_proposal(changed).encode()).digest() != hashlib.sha256(serialized.encode()).digest()


def test_request_identity_binds_every_trusted_field():
    req = request()
    p = proposal(req)
    for field, value in [("board_id", "board-B"), ("anchor_id", "anchor-B"),
        ("repository_identity", "repo-B"), ("base_sha", "d"*40), ("snapshot_hash", "e"*64),
        ("root_contract_hash", "f"*64),
        ("authorized_paths", frozenset({"src/a.py"})), ("max_tranches", 1), ("max_tickets", 3),
        ("max_context_tokens", 2048), ("max_patch_files", 1), ("max_patch_lines", 50)]:
        with pytest.raises(PlannerError):
            parse_proposal(serialize_proposal(p), dataclasses.replace(req, **{field: value}))
    subset = dataclasses.replace(req, expected_criteria=frozenset({"AC-1"}), criterion_statements=(("AC-1", "First criterion"),))
    with pytest.raises(PlannerError):
        parse_proposal(serialize_proposal(p), subset)


def test_strict_duplicate_unknown_and_resource_limits():
    req = request(); raw = serialize_proposal(proposal(req))
    with pytest.raises(PlannerError): parse_proposal(raw.replace('"plan_id":"plan-1"', '"plan_id":"plan-1","plan_id":"plan-1"'), req)
    doc = json.loads(raw); doc["surprise"] = True
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc), req)
    with pytest.raises(PlannerError): parse_proposal(raw + " " * req.max_payload_bytes, req)
    with pytest.raises(PlannerError): parse_proposal("{" * 40 + "}" * 40, req)


def test_missing_full_extra_coverage_and_empty_tranche_are_rejected():
    req = request(); raw = json.loads(serialize_proposal(proposal(req)))
    for coverage in ({"AC-1": ["TK-A"]}, {"AC-1": ["TK-A"], "AC-2": ["TK-B"], "AC-3": ["TK-B"]}):
        changed = dict(raw); changed["plan"]["criterion_coverage"] = coverage
        with pytest.raises(PlannerError): parse_proposal(json.dumps(changed), req)
    raw["plan"]["tranches"][1]["tickets"] = []
    with pytest.raises(PlannerError): parse_proposal(json.dumps(raw), req)


def test_bounds_scope_commands_context_patch_and_paths_cannot_widen():
    req = request(); raw = json.loads(serialize_proposal(proposal(req)))
    mutations = [lambda t: dataclasses.replace(t, allowed_paths=("other.py",)),
                 lambda t: dataclasses.replace(t, allowed_paths=("src/a.py", "other.py")),
                 lambda t: dataclasses.replace(t, verification=VerificationProfile((("sh", "-c", "true"),), 60, 20000)),
                 lambda t: dataclasses.replace(t, patch_budget=PatchBudget(9, 9999, 2)),
                 lambda t: dataclasses.replace(t, context_budget_tokens=99999)]
    for mutate in mutations:
        plan = proposal(req).plan; first = plan.tranches[0]; altered = mutate(first.tickets[0])
        bad = dataclasses.replace(plan, tranches=(dataclasses.replace(first, tickets=(altered,)), plan.tranches[1]))
        with pytest.raises(PlannerError): parse_proposal(serialize_proposal(dataclasses.replace(proposal(req), plan=bad)), req)
    with pytest.raises(ValueError): PlanningRequest(**{**dataclasses.asdict(req), "base_sha": "A"*40})


def test_cycle_future_edges_ordering_and_ordinals():
    req = request(); p = proposal(req)
    assert topological_ticket_order(p.plan) == ("TK-A", "TK-B")
    a = p.plan.tranches[0].tickets[0]; b = p.plan.tranches[1].tickets[0]
    cycle = dataclasses.replace(p.plan, tranches=(dataclasses.replace(p.plan.tranches[0], tickets=(dataclasses.replace(a, dependencies=("TK-B",)),)), p.plan.tranches[1]))
    with pytest.raises(PlannerError): parse_proposal(serialize_proposal(dataclasses.replace(p, plan=cycle)), req)
    serialized = json.loads(serialize_proposal(p))
    serialized["plan"]["tranches"].reverse()
    with pytest.raises(PlannerError): parse_proposal(json.dumps(serialized), req)


def test_active_tranche_is_validated_order_not_model_field_and_import_graph_is_pure():
    req = request(); p = proposal(req)
    assert not hasattr(p, "active_tranche")
    script = '''import sys, local_first_orchestrator.decomposition_planner as m
assert m.__file__.endswith("decomposition_planner.py")
assert not {"subprocess", "local_first_orchestrator.ledger", "local_first_orchestrator.controller", "local_first_orchestrator.paid_model"} & set(sys.modules)
'''
    done = subprocess.run((sys.executable, "-c", script), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_terra_schema_is_real_and_accepts_own_serialized_proposal():
    req = request(); raw = json.loads(serialize_proposal(proposal(req)))
    schema=__import__("local_first_orchestrator.decomposition_planner",fromlist=["planner_schema"]).planner_schema(req)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    raw["plan"]["tranches"][0]["tickets"][0]["verification"]["timeout_seconds"] = "60"
    assert list(Draft202012Validator(schema).iter_errors(raw))


def test_terra_strict_parse_and_execution_order_and_semantic_evidence():
    req=request(); raw=json.loads(serialize_proposal(proposal(req)))
    assert "execution_order" in raw and "objective" in raw["plan"]["tranches"][0]
    for path, value in [(("plan","plan_id"),"../escape"), (("plan","schema_version"),True),
                        (("plan","tranches",0,"ordinal"),True), (("plan","tranches",0,"tranche_id"),""),
                        (("plan","tranches",0,"criterion_ids",0),"")]:
        bad=json.loads(json.dumps(raw)); target=bad
        for key in path[:-1]: target=target[key]
        target[path[-1]]=value
        with pytest.raises(PlannerError): parse_proposal(json.dumps(bad),req)
    with pytest.raises(PlannerError): parse_proposal('{"x":"\\ud800"}',req)


def test_terra_v1_decomposition_digest_is_legacy_payload_exact():
    t=ticket("TK",("AC-1",),("src/a.py",))
    p=DecompositionPlan("plan",1,(TranchePlan("TR",0,(t,),("AC-1",)),),{"AC-1":("TK",)})
    legacy={"schema_version":1,"plan_id":"plan","tranches":[{"tranche_id":"TR","ordinal":0,"criterion_ids":["AC-1"],"tickets":[__import__("local_first_orchestrator.ticket",fromlist=["contract_payload"]).contract_payload(t)]}],"criterion_coverage":{"AC-1":["TK"]}}
    expected=hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    assert p.contract_hash == expected


def test_planner_schema_is_draft_valid_for_generic_empty_and_bound_non_goals():
    from local_first_orchestrator.decomposition_planner import planner_schema
    Draft202012Validator.check_schema(planner_schema())
    empty = request(non_goals=())
    Draft202012Validator.check_schema(planner_schema(empty))
    bound = request()
    Draft202012Validator.check_schema(planner_schema(bound))
    assert "allOf" not in planner_schema()["properties"]["plan"]["properties"]["tranches"]["items"]
    assert "allOf" not in planner_schema(empty)["properties"]["plan"]["properties"]["tranches"]["items"]
    assert planner_schema(bound)["properties"]["plan"]["properties"]["tranches"]["items"]["allOf"]


def test_proposal_rejects_ticket_or_tranche_that_drops_authoritative_root_non_goal():
    req = request()
    for target in ("tranche", "ticket"):
        raw = json.loads(serialize_proposal(proposal(req)))
        if target == "tranche":
            raw["plan"]["tranches"][0]["non_goals"] = []
        else:
            raw["plan"]["tranches"][0]["tickets"][0]["non_goals"] = []
        with pytest.raises(PlannerError, match="root non-goals"):
            parse_proposal(json.dumps(raw, sort_keys=True, separators=(",", ":")), req)
    assert parse_proposal(serialize_proposal(proposal(req)), req) == proposal(req)


@pytest.mark.parametrize("field", ["max_context_tokens", "max_patch_files", "max_patch_lines", "max_attempts", "verification_timeout_seconds", "verification_output_limit", "max_tranches", "max_tickets", "max_payload_bytes", "max_json_depth"])
def test_every_hard_cap_rejects_huge_integer(field):
    with pytest.raises(ValueError):
        request(**{field: 10**100})


@pytest.mark.parametrize("field,value", [("objective", "Other objective"), ("non_goals", ("Different exclusion",)), ("criterion_statements", (("AC-1", "Changed one"), ("AC-2", "Changed two")))])
def test_request_semantics_are_identity_bound(field, value):
    req = request(); p = proposal(req)
    changed = dataclasses.replace(req, **{field: value})
    assert changed.identity != req.identity
    with pytest.raises(PlannerError): parse_proposal(serialize_proposal(p), changed)


def test_tranche_semantics_change_proposal_hash_but_not_request_identity():
    req = request(); p = proposal(req)
    changed = dataclasses.replace(p, tranche_semantics=(TrancheSemantics("TR-A", "Changed", ("No unrelated changes",)), p.tranche_semantics[1]))
    assert changed.request_identity == p.request_identity
    assert changed.proposal_hash != p.proposal_hash


@pytest.mark.parametrize("execution", [[], ["TK-A"], ["TK-A", "TK-A"], ["TK-B", "TK-A"]])
def test_execution_order_must_be_exact(execution):
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); doc["execution_order"]=execution
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc), req)


@pytest.mark.parametrize("path", [("plan",), ("plan", "tranches", 0), ("plan", "tranches", 0, "tickets", 0), ("plan", "tranches", 0, "tickets", 0, "patch_budget"), ("plan", "tranches", 0, "tickets", 0, "verification")])
def test_unknown_nested_fields_rejected(path):
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); target=doc
    for key in path: target=target[key]
    target["unknown"]=1
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc), req)


@pytest.mark.parametrize("raw", ['{"x":"\\ud800"}', '{"x":"\\udc00"}', '{"x":"NaN"}', '{"x":NaN}', '{"x":Infinity}'])
def test_invalid_surrogates_and_nonfinite_json_are_controlled(raw):
    with pytest.raises(PlannerError): parse_proposal(raw, request())


def test_schema_binds_limits_paths_commands_and_working_directory():
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(verification_timeout_seconds=3600, verification_output_limit=16_000_000)
    doc=json.loads(serialize_proposal(proposal(request())))
    doc["request_identity"]=req.identity
    t=doc["plan"]["tranches"][0]["tickets"][0]
    t["verification"]["timeout_seconds"]=3600; t["verification"]["output_limit"]=16_000_000
    schema=planner_schema(req); Draft202012Validator(schema).validate(doc)
    for key,value in [("working_directory", ".."), ("commands", [["sh", "-c", "true"]]), ("output_limit", 16_000_001)]:
        bad=json.loads(json.dumps(doc)); bad["plan"]["tranches"][0]["tickets"][0]["verification"][key]=value
        assert list(Draft202012Validator(schema).iter_errors(bad))


def test_topology_prioritizes_all_ready_current_tranche_tickets():
    first=ticket("TK-A", ("AC-1",), ("src/a.py",))
    second=ticket("TK-B", ("AC-2",), ("tests/a.py",), ("TK-A",))
    future=ticket("TK-C", ("AC-2",), ("tests/a.py",))
    plan=DecompositionPlan("plan",1,(TranchePlan("TR-A",0,(first,second),("AC-1",)),TranchePlan("TR-B",1,(future,),("AC-2",))),{"AC-1":("TK-A",),"AC-2":("TK-B","TK-C")})
    assert topological_ticket_order(plan)==("TK-A","TK-B","TK-C")


@pytest.mark.parametrize("field,value", [("base_sha", None), ("snapshot_hash", 1), ("root_contract_hash", []),
    ("objective", None), ("non_goals", None), ("criterion_statements", None),
    ("non_goals", (1,)), ("criterion_statements", (("AC-1", 2), ("AC-2", "ok")))])
def test_request_bad_types_are_controlled(field, value):
    with pytest.raises((ValueError, TypeError)):
        request(**{field:value})


def test_wrong_request_object_and_plan_identity_types_are_controlled():
    with pytest.raises(PlannerError): parse_proposal("{}", None)
    with pytest.raises(ValueError): PlanProposal(None, proposal(request()).plan, proposal(request()).tranche_semantics)
    with pytest.raises(ValueError): TrancheSemantics("../bad", "objective", ())


@pytest.mark.parametrize("key,value", [("context_budget_tokens",4097),("max_files",3),("max_changed_lines",101),("max_attempts",3),("timeout_seconds",61),("output_limit",20001)])
def test_schema_and_parser_enforce_each_ticket_policy_ceiling(key,value):
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); t=doc["plan"]["tranches"][0]["tickets"][0]
    if key in ("context_budget_tokens",): t[key]=value
    elif key in ("max_files","max_changed_lines","max_attempts"): t["patch_budget"][key]=value
    else: t["verification"][key]=value
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(doc))
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


def test_schema_coverage_requires_expected_keys_and_rejects_duplicate_values():
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); schema=planner_schema(req)
    doc["plan"]["criterion_coverage"].pop("AC-2")
    assert list(Draft202012Validator(schema).iter_errors(doc))
    doc=json.loads(serialize_proposal(proposal(req))); doc["plan"]["criterion_coverage"]["AC-1"]=["TK-A","TK-A"]
    assert list(Draft202012Validator(schema).iter_errors(doc))
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


def test_json_large_integer_is_normalized_and_missing_order_rejected():
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); doc.pop("execution_order")
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)
    with pytest.raises(PlannerError): parse_proposal('{"n":'+"9"*5000+'}',req)


def test_terra_criterion_ids_preserve_spaces_and_scope_schema():
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(expected_criteria=frozenset({"criterion with spaces"}), criterion_statements=(("criterion with spaces", "Statement"),))
    doc=json.loads(serialize_proposal(proposal(request())))
    doc["request_identity"]=req.identity
    doc["plan"]["criterion_coverage"]={"criterion with spaces":["TK-A"]}
    doc["plan"]["tranches"][0]["criterion_ids"]=["criterion with spaces"]
    doc["plan"]["tranches"][0]["tickets"][0]["criterion_ids"]=["criterion with spaces"]
    # Drop the second tranche and ticket so the proposal covers the one requested criterion.
    doc["plan"]["tranches"]=doc["plan"]["tranches"][:1]
    doc["plan"]["tranches"][0]["tickets"][0]["criterion_ids"]=["criterion with spaces"]
    doc["execution_order"]=["TK-A"]
    assert not list(Draft202012Validator(planner_schema(req)).iter_errors(doc))
    assert parse_proposal(json.dumps(doc),req)
    for shape in ("ticket", "tranche"):
        bad=json.loads(json.dumps(doc))
        target=bad["plan"]["tranches"][0]["tickets"][0] if shape=="ticket" else bad["plan"]["tranches"][0]
        target["criterion_ids"]=["OUT-OF-SCOPE"]
        assert list(Draft202012Validator(planner_schema(req)).iter_errors(bad))
        with pytest.raises(PlannerError): parse_proposal(json.dumps(bad),req)


@pytest.mark.parametrize("mutation", ["criterion_ids", "non_goals", "allowed_paths", "dependencies", "command", "objective"])
@pytest.mark.parametrize("bad_value", [[], {}, None, True, 1])
def test_terra_bad_array_members_and_text_are_controlled(mutation,bad_value):
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); t=doc["plan"]["tranches"][0]["tickets"][0]
    if mutation=="criterion_ids": doc["plan"]["tranches"][0]["criterion_ids"]=[bad_value]
    elif mutation=="non_goals": t["non_goals"]=[bad_value]
    elif mutation=="allowed_paths": t["allowed_paths"]=[bad_value]
    elif mutation=="dependencies": t["dependencies"]=[bad_value]
    elif mutation=="command": t["verification"]["commands"]=[[bad_value]]
    else: t["objective"]=bad_value
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


@pytest.mark.parametrize("field,value", [("objective","x"*4097),("non_goals",["x"]*1025)])
def test_terra_ticket_text_schema_limits_match_parser(field,value):
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); t=doc["plan"]["tranches"][0]["tickets"][0]; t[field]=value
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(doc))
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


@pytest.mark.parametrize("field,values", [
    ("criterion_ids", [f"AC-{i}" for i in range(16385)]),
    ("non_goals", ["excluded"]*1025),
    ("allowed_paths", [f"src/file-{i}.py" for i in range(2049)]),
    ("dependencies", [f"TK-{i}" for i in range(2049)]),
])
def test_terra_schema_capped_ticket_arrays_rejected_by_schema_and_parser(field,values):
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(max_payload_bytes=4_000_000); doc=json.loads(serialize_proposal(proposal(request()))); t=doc["plan"]["tranches"][0]["tickets"][0]
    t[field]=values
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(doc))
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


def test_terra_ticket_text_and_non_goal_count_boundaries_are_accepted():
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(max_payload_bytes=4_000_000); doc=json.loads(serialize_proposal(proposal(req))); t=doc["plan"]["tranches"][0]["tickets"][0]
    t["objective"]="x"*4096; t["non_goals"]=[req.non_goals[0], *[f"excluded-{i}" for i in range(1023)]]
    Draft202012Validator(planner_schema(req)).validate(doc)
    assert parse_proposal(json.dumps(doc),req)


def test_terra_ticket_identifier_length_limit_matches_schema_and_parser():
    from local_first_orchestrator.decomposition_planner import planner_schema
    req=request(); doc=json.loads(serialize_proposal(proposal(req))); doc["plan"]["tranches"][0]["tickets"][0]["ticket_id"]="T"*129
    assert list(Draft202012Validator(planner_schema(req)).iter_errors(doc))
    with pytest.raises(PlannerError): parse_proposal(json.dumps(doc),req)


@pytest.mark.parametrize("commands", [None,(None,),[],["python"],((1,),)])
def test_terra_malformed_trusted_verification_commands_are_value_errors(commands):
    with pytest.raises(ValueError): request(verification_commands=commands)
