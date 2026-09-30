import copy
import dataclasses
import hashlib
import json
import subprocess
import sys

import pytest

from local_first_orchestrator.decomposition import DecompositionPlan, TranchePlan
from local_first_orchestrator.decomposition_planner import PlanningRequest, PlanProposal, TrancheSemantics, serialize_proposal, topological_ticket_order
from local_first_orchestrator.planning_coordinator import evidence_payload, first_active_tranche_materialization, reconstruct_evidence
from local_first_orchestrator.ticket import PatchBudget, TicketContract, VerificationProfile


def fixture():
    req = PlanningRequest(board_id="board-A", anchor_id="anchor-A", repository_identity="repo-A", base_sha="a"*40, snapshot_hash="b"*64, root_contract_hash="c"*64, expected_criteria=frozenset({"AC-1", "AC-2"}), authorized_paths=frozenset({"src/a.py", "src/b.py", "tests/c.py"}), max_tranches=2, max_tickets=4, max_context_tokens=4096, max_patch_files=2, max_patch_lines=100, max_attempts=2, verification_commands=(("python", "-m", "pytest"),), verification_timeout_seconds=60, verification_output_limit=20000, max_payload_bytes=65536, max_json_depth=16, objective="Deliver bounded change", non_goals=("No unrelated changes",), criterion_statements=(("AC-1", "First criterion"), ("AC-2", "Second criterion")))
    v = VerificationProfile((("python", "-m", "pytest"),), 60, 20000)
    budget = PatchBudget(2, 100, 2)
    a = TicketContract("TK-A", "Implement bounded behavior", ("AC-1",), ("No unrelated changes",), ("src/a.py",), v, budget, 4096, ())
    b = TicketContract("TK-B", "Implement bounded behavior", ("AC-1",), ("No unrelated changes",), ("src/b.py",), v, budget, 4096, ())
    future = TicketContract("TK-C", "Implement bounded behavior", ("AC-2",), ("No unrelated changes",), ("tests/c.py",), v, budget, 4096, ("TK-A",))
    plan = DecompositionPlan("plan-1", 1, (TranchePlan("TR-A", 0, (b, a), ("AC-1",)), TranchePlan("TR-B", 1, (future,), ("AC-2",))), {"AC-1": ("TK-A", "TK-B"), "AC-2": ("TK-C",)})
    proposal = PlanProposal(req.identity, plan, (TrancheSemantics("TR-A", "First", ("No unrelated changes",)), TrancheSemantics("TR-B", "Later", ("No unrelated changes",))))
    return req, proposal, evidence_payload(req, proposal, planner_task_id="planner", planner_run_id="run", planner_session_id="session", planner_profile="paid-planner")


def route(**changes):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    return ActiveTrancheRoute(**{"implementation_profile": "local-coder", "workspace": "/work/repo", **changes})


def test_materializes_only_validated_first_tranche_in_topological_order_and_exact_identities():
    req, p, evidence = fixture()
    before = copy.deepcopy(evidence)
    result = first_active_tranche_materialization(evidence, route())
    assert result.active_tranche_id == "TR-A" and result.active_tranche_ordinal == 0
    assert tuple(t.ticket_id for t in result.targets) == tuple(x for x in topological_ticket_order(p.plan) if x in {"TK-A", "TK-B"})
    assert [t.criterion_ids for t in result.targets] == [("AC-1",), ("AC-1",)]
    assert result.source["schema_version"] == 1 and result.source["request_identity"] == req.identity
    assert result.source["proposal_hash"] == p.proposal_hash and result.source["plan_contract_hash"] == p.plan.contract_hash
    assert result.source["planner"] == evidence["planner"]
    assert evidence == before
    for t in result.targets:
        assert t.tranche_id == "TR-A" and t.tranche_ordinal == 0 and t.implementation_profile == "local-coder"
        assert t.workspace == "dir:/work/repo" and t.native_parent is False and t.native_dependencies() == ()
        assert t.initial_status == "blocked" and isinstance(t.declared_dependencies, tuple)
        assoc = {"kind":"active_tranche_piece_v1", "board_id":"board-A", "anchor_task_id":"anchor-A", "plan_id":"plan-1", "request_identity":req.identity, "proposal_hash":p.proposal_hash, "plan_contract_hash":p.plan.contract_hash, "tranche_id":"TR-A", "tranche_ordinal":0, "ticket_id":t.ticket_id, "ticket_contract_hash":next(x for tr in p.plan.tranches for x in tr.tickets if x.ticket_id==t.ticket_id).contract_hash}
        digest = hashlib.sha256(json.dumps(assoc, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        assert t.association == "active-tranche-piece:" + digest
        op = {"kind":"active_tranche_held_create_v1", "association":t.association, "implementation_profile":"local-coder", "workspace":"dir:/work/repo", "declared_dependencies":list(t.declared_dependencies)}
        assert t.operation_key == "active-tranche-held:" + hashlib.sha256(json.dumps(op, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    with pytest.raises((AttributeError, TypeError)):
        result.source["planner"]["profile"] = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        result.targets[0].criterion_ids[0] = "mutated"


def test_later_tranche_is_never_selected_and_source_only_changes_for_planner_provenance():
    _, _, ev = fixture()
    original = first_active_tranche_materialization(ev, route())
    ev2 = copy.deepcopy(ev); ev2["planner"]["run_id"] = "run-2"
    changed = first_active_tranche_materialization(ev2, route())
    assert changed.source != original.source
    assert [(x.association, x.operation_key) for x in changed.targets] == [(x.association, x.operation_key) for x in original.targets]


@pytest.mark.parametrize("workspace", ["", "relative", "/work/../repo", "/work/./repo", "/work//repo", "/work/repo/", "/", "/work/./", "/work/../"])
def test_rejects_noncanonical_workspace(workspace):
    _, _, ev = fixture()
    with pytest.raises((TypeError, ValueError)):
        first_active_tranche_materialization(ev, route(workspace=workspace))


@pytest.mark.parametrize("profile", ["", " ", None, 1, True])
def test_rejects_bad_profile(profile):
    _, _, ev = fixture()
    with pytest.raises((TypeError, ValueError)):
        first_active_tranche_materialization(ev, route(implementation_profile=profile))


@pytest.mark.parametrize("field,value", [
    ("implementation_profile", None), ("implementation_profile", True),
    ("implementation_profile", " "), ("implementation_profile", "bad\ud800"),
    ("workspace", "relative"), ("workspace", "dir:relative"),
    ("workspace", None), ("workspace", True), ("workspace", "/work/bad\ud800"),
])
def test_constructor_bypass_route_fields_are_revalidated(field, value):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    candidate = object.__new__(ActiveTrancheRoute)
    object.__setattr__(candidate, "implementation_profile", "local-coder")
    object.__setattr__(candidate, "workspace", "/work/repo")
    object.__setattr__(candidate, field, value)
    with pytest.raises(ValueError):
        first_active_tranche_materialization(ev, candidate)


@pytest.mark.parametrize("missing", ["implementation_profile", "workspace"])
def test_constructor_bypass_missing_route_field_is_controlled(missing):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    candidate = object.__new__(ActiveTrancheRoute)
    object.__setattr__(candidate, "implementation_profile", "local-coder")
    object.__setattr__(candidate, "workspace", "/work/repo")
    object.__delattr__(candidate, missing)
    with pytest.raises(ValueError):
        first_active_tranche_materialization(ev, candidate)


@pytest.mark.parametrize("field,value", [("implementation_profile", "bad\ud800"), ("workspace", "/work/bad\ud800")])
def test_route_constructor_rejects_non_utf8_unicode(field, value):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    with pytest.raises(ValueError):
        ActiveTrancheRoute(**{field: value, **({"workspace": "/work/repo"} if field == "implementation_profile" else {"implementation_profile": "local-coder"})})


@pytest.mark.parametrize("control", ["\x00", "\n", "\t", "\x7f", "\x85"])
@pytest.mark.parametrize("field", ["implementation_profile", "workspace"])
def test_route_constructor_rejects_control_characters(field, control):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    value = "local-coder" + control if field == "implementation_profile" else "/work/" + control + "repo"
    with pytest.raises(ValueError):
        ActiveTrancheRoute(**{field: value, **({"workspace": "/work/repo"} if field == "implementation_profile" else {"implementation_profile": "local-coder"})})


@pytest.mark.parametrize("field,value", [("implementation_profile", " local-coder "), ("implementation_profile", "local\n-coder"), ("workspace", " /work/repo"), ("workspace", "/work/repo ")])
def test_route_constructor_rejects_surrounding_whitespace(field, value):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    with pytest.raises(ValueError):
        ActiveTrancheRoute(**{field: value, **({"workspace": "/work/repo"} if field == "implementation_profile" else {"implementation_profile": "local-coder"})})


@pytest.mark.parametrize("control", ["\x00", "\n", "\t", "\x7f", "\x85"])
@pytest.mark.parametrize("field", ["implementation_profile", "workspace"])
def test_constructor_bypass_control_characters_are_rejected_at_consumption(field, control):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    candidate = object.__new__(ActiveTrancheRoute)
    object.__setattr__(candidate, "implementation_profile", "local-coder")
    object.__setattr__(candidate, "workspace", "/work/repo")
    object.__setattr__(candidate, field, "local-coder" + control if field == "implementation_profile" else "/work/" + control + "repo")
    with pytest.raises(ValueError):
        first_active_tranche_materialization(ev, candidate)


@pytest.mark.parametrize("field,value", [("implementation_profile", " local-coder "), ("implementation_profile", "local\n-coder"), ("workspace", " /work/repo"), ("workspace", "/work/repo ")])
def test_constructor_bypass_whitespace_is_rejected_at_consumption(field, value):
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    candidate = object.__new__(ActiveTrancheRoute)
    object.__setattr__(candidate, "implementation_profile", "local-coder")
    object.__setattr__(candidate, "workspace", "/work/repo")
    object.__setattr__(candidate, field, value)
    with pytest.raises(ValueError):
        first_active_tranche_materialization(ev, candidate)


def test_workspace_with_interior_spaces_and_unicode_remains_canonical():
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    route_value = ActiveTrancheRoute("cödér", "/work/my repo/répo")
    first = first_active_tranche_materialization(ev, route_value)
    second = first_active_tranche_materialization(ev, route_value)
    assert first.targets[0].workspace == "dir:/work/my repo/répo"
    assert [(t.association, t.operation_key) for t in first.targets] == [(t.association, t.operation_key) for t in second.targets]


def test_valid_unicode_route_is_utf8_stable():
    from local_first_orchestrator.planning_coordinator import ActiveTrancheRoute
    _, _, ev = fixture()
    unicode_route = ActiveTrancheRoute("cödér", "/work/répo")
    first = first_active_tranche_materialization(ev, unicode_route)
    second = first_active_tranche_materialization(ev, unicode_route)
    assert [(t.association, t.operation_key) for t in first.targets] == [(t.association, t.operation_key) for t in second.targets]
    assert first.targets[0].workspace == "dir:/work/répo"


def test_rejects_planner_profile_as_implementation_profile():
    _, _, ev = fixture()
    with pytest.raises(ValueError): first_active_tranche_materialization(ev, route(implementation_profile="paid-planner"))


def test_stateful_dict_subclasses_are_rejected_without_invoking_hooks():
    _, _, ev = fixture()

    class HostileDict(dict):
        called = False
        def items(self):
            type(self).called = True
            raise AssertionError("items hook invoked")
        def __getitem__(self, key):
            type(self).called = True
            raise AssertionError("getitem hook invoked")

    attacked = HostileDict(ev)
    with pytest.raises(ValueError):
        first_active_tranche_materialization(attacked, route())
    assert HostileDict.called is False


def test_snapshot_rejects_wide_exact_mapping_before_iteration():
    from local_first_orchestrator.planning_coordinator import _evidence_snapshot
    attacked = {str(i): None for i in range(100001)}
    with pytest.raises(ValueError):
        _evidence_snapshot(attacked)


def test_snapshot_enforces_global_node_budget_across_small_containers():
    from local_first_orchestrator.planning_coordinator import _evidence_snapshot
    nested = [None] * 16000
    value = {"level": nested}
    for _ in range(17):
        value = {"level": value, "padding": [None] * 16000}
    with pytest.raises(ValueError):
        _evidence_snapshot(value)


def test_tampered_or_malformed_evidence_is_rejected():
    _, _, ev = fixture()
    for bad in ({**ev, "proposal_hash":"bad"}, {**ev, "request_identity":"bad"}, {**ev, "schema_version":True}, {**ev, "planner":{**ev["planner"], "profile":""}}):
        with pytest.raises((ValueError, TypeError)): first_active_tranche_materialization(bad, route())
    raw = copy.deepcopy(ev); raw["proposal"]["plan"]["tranches"][0]["tickets"][0]["objective"] = "tampered"
    with pytest.raises((ValueError, TypeError)): first_active_tranche_materialization(raw, route())


def test_base_request_proposal_and_ticket_revisions_change_or_reject_identity():
    _, _, ev = fixture(); baseline = first_active_tranche_materialization(ev, route())
    for edit in (lambda x: x["request"].update(base_sha="d"*40), lambda x: x["request"].update(board_id="other"), lambda x: x["proposal"]["plan"].update(plan_id="other")):
        changed=copy.deepcopy(ev); edit(changed)
        with pytest.raises((ValueError, TypeError)): first_active_tranche_materialization(changed, route())
    _, p, _ = fixture()
    t = p.plan.tranches[0].tickets[0]
    altered = dataclasses.replace(t, objective="Revised objective")
    tr = dataclasses.replace(p.plan.tranches[0], tickets=(altered, p.plan.tranches[0].tickets[1]))
    badplan=dataclasses.replace(p.plan, tranches=(tr,p.plan.tranches[1]))
    badproposal=dataclasses.replace(p,plan=badplan)
    req,_,_=fixture(); bad=evidence_payload(req,badproposal,planner_task_id="planner",planner_run_id="run",planner_session_id="session",planner_profile="paid-planner")
    newer=first_active_tranche_materialization(bad,route())
    old_target = next(x for x in baseline.targets if x.ticket_id == "TK-B")
    new_target = next(x for x in newer.targets if x.ticket_id == "TK-B")
    assert new_target.ticket_contract_hash != old_target.ticket_contract_hash
    assert new_target.association != old_target.association


def test_anchor_ticket_and_anchor_dependency_are_rejected():
    from local_first_orchestrator.decomposition_planner import parse_proposal
    req,p,_=fixture()
    for old,new in (("TK-A","anchor-A"),):
        t=next(t for tr in p.plan.tranches for t in tr.tickets if t.ticket_id==old)
        alt=dataclasses.replace(t,ticket_id=new)
        trs=tuple(dataclasses.replace(tr,tickets=tuple(alt if x is t else dataclasses.replace(x, dependencies=tuple(new if d == old else d for d in x.dependencies)) for x in tr.tickets)) for tr in p.plan.tranches)
        coverage={k:tuple(new if x==old else x for x in v) for k,v in p.plan.criterion_coverage.items()}
        proposal=dataclasses.replace(p,plan=dataclasses.replace(p.plan,tranches=trs,criterion_coverage=coverage))
        evidence=evidence_payload(req,proposal,planner_task_id="planner",planner_run_id="run",planner_session_id="session",planner_profile="paid-planner")
        with pytest.raises(ValueError): first_active_tranche_materialization(evidence,route())
    t=p.plan.tranches[0].tickets[0]; anchor_ticket=dataclasses.replace(t,ticket_id="anchor-A",dependencies=())
    dependent=dataclasses.replace(p.plan.tranches[0].tickets[1],dependencies=("anchor-A",))
    trs=(dataclasses.replace(p.plan.tranches[0],tickets=(anchor_ticket,dependent)),p.plan.tranches[1])
    coverage={"AC-1":("anchor-A", dependent.ticket_id), "AC-2":("TK-C",)}
    with pytest.raises(ValueError):
        pp=dataclasses.replace(p,plan=dataclasses.replace(p.plan,tranches=trs,criterion_coverage=coverage))
        evidence=evidence_payload(req,pp,planner_task_id="planner",planner_run_id="run",planner_session_id="session",planner_profile="paid-planner")
        first_active_tranche_materialization(evidence,route())


def test_pure_import_surface_has_no_effect_or_store_modules():
    script = 'import sys; import local_first_orchestrator.planning_coordinator; forbidden={"sqlite3","local_first_orchestrator.evidence_store","local_first_orchestrator.hermes_board","local_first_orchestrator.ledger","local_first_orchestrator.controller"}; assert not (forbidden & set(sys.modules))'
    done=subprocess.run((sys.executable,"-c",script),capture_output=True,text=True)
    assert done.returncode==0,done.stderr


def test_snapshot_utf8_byte_boundaries_shared_across_mapping_keys_and_values(monkeypatch):
    import local_first_orchestrator.planning_coordinator as coordinator
    monkeypatch.setattr(coordinator, "_SNAPSHOT_MAX_UTF8_BYTES", 8)
    monkeypatch.setattr(coordinator, "_SNAPSHOT_MAX_STRING_CHARS", 100)
    assert coordinator._evidence_snapshot({"éé": "😀"}) == {"éé": "😀"}  # 4 + 4 bytes
    assert coordinator._evidence_snapshot({"12345678": ""}) == {"12345678": ""}
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        coordinator._evidence_snapshot({"123456789": ""})
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        coordinator._evidence_snapshot({"éé": "😀x"})
    assert coordinator._bounded_utf8_byte_count("aé中😀", 10) == 10
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        coordinator._bounded_utf8_byte_count("aé中😀", 9)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        coordinator._bounded_utf8_byte_count("ok\ud800", 20)


def test_snapshot_utf8_default_cap_accepts_exact_near_max_multibyte_string():
    from local_first_orchestrator.planning_coordinator import _evidence_snapshot
    value = "é" * 2_000_000
    snapshot = _evidence_snapshot(value)
    assert snapshot is value
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        _evidence_snapshot(value + "a")


def test_snapshot_depth_boundaries_and_cycles():
    from local_first_orchestrator.planning_coordinator import _evidence_snapshot
    exact = None
    for _ in range(32):
        exact = [exact]
    assert _evidence_snapshot(exact) == exact
    too_deep = [exact]
    with pytest.raises(ValueError, match="nesting limit"):
        _evidence_snapshot(too_deep)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="nesting limit"):
        _evidence_snapshot(cycle)


def test_snapshot_container_cardinality_checked_before_children(monkeypatch):
    import local_first_orchestrator.planning_coordinator as coordinator
    monkeypatch.setattr(coordinator, "_SNAPSHOT_MAX_CONTAINER_ITEMS", 2)
    class HostileString(str):
        def __iter__(self):
            raise AssertionError("child traversal must not occur")
    with pytest.raises(ValueError, match="item limit"):
        coordinator._evidence_snapshot([HostileString("x")] * 3)
    with pytest.raises(ValueError, match="item limit"):
        coordinator._evidence_snapshot({"a": 1, "b": 2, "c": 3})


def test_snapshot_rejects_string_and_integer_subclasses_without_hooks():
    from local_first_orchestrator.planning_coordinator import _evidence_snapshot
    class HostileString(str):
        called = False
        def encode(self, *args, **kwargs):
            type(self).called = True
            raise AssertionError("encode hook invoked")
    class HostileInt(int):
        called = False
        def __int__(self):
            type(self).called = True
            raise AssertionError("int hook invoked")
    with pytest.raises(ValueError):
        _evidence_snapshot(HostileString("x"))
    with pytest.raises(ValueError):
        _evidence_snapshot(HostileInt(1))
    assert HostileString.called is False
    assert HostileInt.called is False
