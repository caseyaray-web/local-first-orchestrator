from dataclasses import dataclass
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess
from collections.abc import Mapping

import pytest

from local_first_orchestrator.contracts import Action
from local_first_orchestrator.contracts import BoardSnapshot, ManagedMember, OperationIntent
from local_first_orchestrator.evidence_store import EvidenceStore
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.planning_coordinator import (accepted_active_tranche_create_payload,
    evidence_payload, first_active_tranche_materialization, reconstruct_evidence, ActiveTrancheRoute)
from tests.test_hermes_board_adapter import FakeKanban

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}


@dataclass(frozen=True)
class AcceptedDescription:
    title: str
    body: str
    target: dict
    idempotency_key: str
    plan_evidence: dict


def payload():
    req0, proposal, _ = __import__("tests.test_m4_active_tranche_materialization", fromlist=["fixture"]).fixture()
    req = dataclasses.replace(req0, board_id="fixture-board", anchor_id="anchor-1")
    proposal = dataclasses.replace(proposal, request_identity=req.identity)
    evidence = evidence_payload(req, proposal, planner_task_id="fixture-planner", planner_run_id="fixture-run",
        planner_session_id="fixture-session", planner_profile="paid-planner")
    route = {"implementation_profile": "implementer", "workspace": "/repo"}
    token = {"schema_version": 1, "plan_id": proposal.plan.plan_id, "request_identity": req.identity,
        "proposal_hash": proposal.proposal_hash, "plan_contract_hash": proposal.plan.contract_hash,
        "planner": evidence["planner"], "repository_identity": req.repository_identity, "base_sha": req.base_sha,
        "snapshot_hash": req.snapshot_hash, "root_contract_hash": req.root_contract_hash,
        "active_tranche": {"tranche_id": proposal.plan.tranches[0].tranche_id, "ordinal": 0}, "route": route}
    token["acceptance_identity"] = "sha256:" + hashlib.sha256(json.dumps(token, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    material = first_active_tranche_materialization(evidence, ActiveTrancheRoute(**route))
    built = accepted_active_tranche_create_payload(token, evidence, material.targets[0])
    target = {str(k): thaw(v) for k, v in built.target.items()}
    return AcceptedDescription(built.title, built.body, target, built.idempotency_key, json.loads(json.dumps(evidence)))


def thaw(value):
    if isinstance(value, Mapping):
        return {str(k): thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(v) for v in value]
    return value


def make_action(p, **changes):
    target = {**p.target, "task_id": "anchor-1", "anchor_task_id": "anchor-1", "native_parent": False, "native_deps": []}
    target.update(changes)
    return Action(p.idempotency_key, SCOPE, target, "create_held", "anchor-digest")


def adapter(tmp_path, fake, resolver=None, claim=None):
    exe = tmp_path / "hermes"
    exe.write_text("fixture")
    return HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-1", executable=str(exe),
        runner=fake, hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"),
        create_lock_assertion=lambda *_: True, claim_create_attempt=claim or (lambda *_: True),
        accepted_piece_create_lookup=resolver)


@pytest.mark.parametrize("attack", ["extra_top_level", "malformed_proposal_hash"])
def test_accepted_piece_body_and_hash_dead_validator_attacks_rejected(tmp_path, attack):
    from local_first_orchestrator.planning_coordinator import _canonical_digest

    fake = FakeKanban(); fake.add("anchor-1")
    original = payload()
    body_obj = json.loads(original.body)
    target = dict(original.target)
    if attack == "extra_top_level":
        body_obj["mirrored_untrusted"] = "extra"
    else:
        token = body_obj["accepted_token"]
        token["proposal_hash"] = "not-a-sha256-digest"
        token["acceptance_identity"] = "sha256:" + _canonical_digest(
            {k: v for k, v in token.items() if k != "acceptance_identity"})
        source = target["source_hashes"]
        source["proposal_hash"] = token["proposal_hash"]
        source["acceptance_identity"] = token["acceptance_identity"]
        target["accepted_token_key"] = "accept-plan:" + token["acceptance_identity"]
        body_obj["accepted_token_key"] = target["accepted_token_key"]
        expected_assoc = {"kind": "active_tranche_piece_v1", "board_id": SCOPE["board_id"],
            "anchor_task_id": SCOPE["anchor_task_id"], "plan_id": token["plan_id"],
            "request_identity": token["request_identity"], "proposal_hash": token["proposal_hash"],
            "plan_contract_hash": token["plan_contract_hash"],
            "tranche_id": token["active_tranche"]["tranche_id"], "tranche_ordinal": 0,
            "ticket_id": target["ticket_id"], "ticket_contract_hash": target["ticket_contract_hash"]}
        target["association"] = "active-tranche-piece:" + _canonical_digest(expected_assoc)
        operation = {"kind": "active_tranche_held_create_v1", "association": target["association"],
            "implementation_profile": target["assignee"], "workspace": target["workspace"],
            "declared_dependencies": body_obj["ticket"]["dependencies"]}
        target["idempotency_key"] = "active-tranche-held:" + _canonical_digest(operation)
        body_obj["association"]["operation_key"] = target["idempotency_key"]
        target["source_hashes"] = source
    body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target["body"] = body
    attacked = AcceptedDescription(original.title, body, target, target["idempotency_key"], original.plan_evidence)
    board = adapter(tmp_path, fake, lambda *_: attacked)
    result = board.create_held(make_action(attacked), title=attacked.title, body=body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=attacked.idempotency_key)
    assert result.outcome == "conflict"
    expected_detail = "trusted accepted-piece resolution failed"
    assert expected_detail in result.details, result.details
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("field", [
    "repository.repository_identity",
    "criterion_statements.0.statement",
    "tranche_semantics.objective",
])
def test_accepted_piece_reconstructs_semantics_from_unchanged_plan_evidence(tmp_path, field):
    original = payload()
    body_obj = json.loads(original.body)
    target = dict(original.target)
    path = field.split(".")
    value = body_obj
    for part in path[:-1]:
        value = value[int(part)] if part.isdigit() else value[part]
    value[path[-1]] = "forged reconstructed source"
    body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target["body"] = body
    attacked = AcceptedDescription(original.title, body, target, original.idempotency_key, original.plan_evidence)
    fake = FakeKanban(); fake.add("anchor-1")
    board = adapter(tmp_path, fake, lambda *_: attacked)
    action = make_action(attacked)
    result = board.create_held(action, title=attacked.title, body=body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=attacked.idempotency_key)
    assert result.outcome == "conflict", result.details
    assert "reconstruct" in result.details.lower() or "source" in result.details.lower(), result.details
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("field,bad_value", [("request_identity", "not-a-digest"), ("base_sha", "not-a-git-sha")])
def test_accepted_piece_reconstructs_source_identity_formats(tmp_path, field, bad_value):
    from local_first_orchestrator.planning_coordinator import _canonical_digest

    original = payload()
    body_obj = json.loads(original.body)
    target = dict(original.target)
    token = body_obj["accepted_token"]
    token[field] = bad_value
    token["acceptance_identity"] = "sha256:" + _canonical_digest(
        {k: v for k, v in token.items() if k != "acceptance_identity"})
    source = target["source_hashes"]
    source[field] = bad_value
    source["acceptance_identity"] = token["acceptance_identity"]
    target["accepted_token_key"] = "accept-plan:" + token["acceptance_identity"]
    body_obj["accepted_token_key"] = target["accepted_token_key"]
    association = {"kind": "active_tranche_piece_v1", "board_id": SCOPE["board_id"],
        "anchor_task_id": SCOPE["anchor_task_id"], "plan_id": token["plan_id"],
        "request_identity": token["request_identity"], "proposal_hash": token["proposal_hash"],
        "plan_contract_hash": token["plan_contract_hash"], "tranche_id": token["active_tranche"]["tranche_id"],
        "tranche_ordinal": 0, "ticket_id": target["ticket_id"], "ticket_contract_hash": target["ticket_contract_hash"]}
    target["association"] = "active-tranche-piece:" + _canonical_digest(association)
    operation = {"kind": "active_tranche_held_create_v1", "association": target["association"],
        "implementation_profile": target["assignee"], "workspace": target["workspace"],
        "declared_dependencies": body_obj["ticket"]["dependencies"]}
    target["idempotency_key"] = "active-tranche-held:" + _canonical_digest(operation)
    body_obj["association"]["operation_key"] = target["idempotency_key"]
    target["source_hashes"] = source
    body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target["body"] = body
    attacked = AcceptedDescription(original.title, body, target, target["idempotency_key"], original.plan_evidence)
    fake = FakeKanban(); fake.add("anchor-1")
    board = adapter(tmp_path, fake, lambda *_: attacked)
    result = board.create_held(make_action(attacked), title=attacked.title, body=body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=attacked.idempotency_key)
    assert result.outcome == "conflict", result.details
    assert "reconstruct" in result.details.lower() or "source" in result.details.lower(), result.details
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_accepted_piece_requires_reconstructable_plan_evidence(tmp_path, damage):
    original = payload()
    fake = FakeKanban(); fake.add("anchor-1")
    if damage == "missing":
        attacked = type("ResolverPayload", (), {k: getattr(original, k) for k in
            ("title", "body", "target", "idempotency_key")})()
    else:
        evidence = json.loads(json.dumps(original.plan_evidence))
        evidence["request"]["request_identity"] = "stale-request-identity"
        attacked = AcceptedDescription(original.title, original.body, original.target,
            original.idempotency_key, evidence)
    board = adapter(tmp_path, fake, lambda *_: attacked)
    result = board.create_held(make_action(original), title=original.title, body=original.body,
        assignee="implementer", workspace="dir:/repo", idempotency_key=original.idempotency_key)
    assert result.outcome == "conflict", result.details
    assert "trusted accepted-piece resolution failed" in result.details
    assert not any(call[4] == "create" for call in fake.calls)


def test_accepted_piece_missing_resolver_fails_before_cli(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1")
    p = payload(); board = adapter(tmp_path, fake)
    result = board.create_held(make_action(p), title=p.title, body=p.body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=p.idempotency_key)
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("damage", ["target_extra", "missing_ticket_id", "null_ticket_id", "ticket_extra", "token_extra"])
def test_accepted_piece_closed_schema_attack_rejected_before_native_create(tmp_path, damage):
    fake = FakeKanban(); fake.add("anchor-1")
    original = payload()
    target = dict(original.target)
    body_obj = json.loads(original.body)
    if damage == "target_extra":
        target["mirrored_untrusted"] = "extra"
    elif damage == "missing_ticket_id":
        target.pop("ticket_id")
        body_obj["ticket"].pop("ticket_id")
    elif damage == "null_ticket_id":
        target["ticket_id"] = None
        body_obj["ticket"]["ticket_id"] = None
    elif damage == "ticket_extra":
        body_obj["ticket"]["unexpected"] = "extra"
    elif damage == "token_extra":
        body_obj["accepted_token"]["unexpected"] = "extra"
    body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target["body"] = body
    attacked = AcceptedDescription(original.title, body, target, original.idempotency_key, original.plan_evidence)
    board = adapter(tmp_path, fake, lambda *_: attacked)
    action = make_action(attacked)
    result = board.create_held(action, title=attacked.title, body=body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=attacked.idempotency_key)
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("change", [
    {"title": "forged"}, {"body": "{}"}, {"assignee": "other"}, {"workspace": "dir:/other"},
    {"idempotency_key": "other"}, {"native_parent": True}, {"native_deps": ["ancestor"]},
    {"role": "reviewer"}, {"ticket_id": "other"},
])
def test_accepted_piece_malformed_action_never_calls_native_create(tmp_path, change):
    fake = FakeKanban(); fake.add("anchor-1")
    p = payload(); board = adapter(tmp_path, fake, lambda scope, key: p)
    result = board.create_held(make_action(p, **change), title=p.title, body=p.body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=p.idempotency_key)
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake.calls)


def test_accepted_piece_create_is_parentless_and_lost_response_reconciles(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1"); fake.create_returncode_after_effect = 9
    p = payload(); resolutions = []
    def resolve(scope, key):
        resolutions.append((dict(scope), key))
        return p
    claims = []
    board = adapter(tmp_path, fake, resolve, lambda scope, key: claims.append(key) or True)
    action = Action(p.idempotency_key, SCOPE, make_action(p).target, "create_held", board.read_task("anchor-1").digest)
    result = board.create_held(action, title=p.title, body=p.body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=p.idempotency_key)
    assert result.outcome == "unknown", result.details
    assert len([c for c in fake.calls if c[4] == "create"]) == 1
    create = next(c for c in fake.calls if c[4] == "create")
    assert "--parent" not in create
    assert "--initial-status" in create and create[create.index("--initial-status") + 1] == "blocked"
    assert claims == [action.key]
    reconciled = board.verify_effect(action)
    assert reconciled.outcome == "no-op", reconciled.details
    assert len([c for c in fake.calls if c[4] == "create"]) == 1
    assert fake.tasks["new-2"]["task"]["status"] == "blocked"
    assert not fake.tasks["new-2"]["parents"]
    assert len(resolutions) >= 2


def test_trusted_resolution_change_under_lock_stops_before_create(tmp_path):
    fake = FakeKanban(); fake.add("anchor-1")
    p = payload(); calls = []
    def resolve(scope, key):
        calls.append((dict(scope), key))
        if len(calls) == 1:
            return p
        changed = payload()
        changed_body = json.loads(changed.body)
        changed_body["ticket"]["id"] = "changed-after-initial-resolution"
        changed_body = json.dumps(changed_body, separators=(",", ":"))
        return AcceptedDescription("ticket-1: implement", changed_body,
            {**changed.target, "body": changed_body}, changed.idempotency_key, changed.plan_evidence)
    board = adapter(tmp_path, fake, resolve)
    action = Action(p.idempotency_key, SCOPE, make_action(p).target, "create_held", board.read_task("anchor-1").digest)
    result = board.create_held(action, title=p.title, body=p.body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=p.idempotency_key)
    assert result.outcome == "conflict"
    assert "under lock" in result.details
    assert len([c for c in fake.calls if c[4] == "create"]) == 0


@pytest.mark.parametrize("damage", ["wrong_kind", "missing_kind", "bool_kind", "extra_field", "body_kind"])
def test_accepted_piece_resolver_closed_contract_fails_before_create(tmp_path, damage):
    fake = FakeKanban(); fake.add("anchor-1")
    p = payload()
    target = dict(p.target)
    body = p.body
    if damage == "wrong_kind": target["kind"] = "other"
    elif damage == "missing_kind": target.pop("kind")
    elif damage == "bool_kind": target["kind"] = True
    elif damage == "extra_field": target["unexpected"] = "value"
    elif damage == "body_kind":
        body = json.dumps({"kind": "other", "schema_version": 1})
        target["body"] = body
    resolved = AcceptedDescription(p.title, body, target, p.idempotency_key, p.plan_evidence)
    board = adapter(tmp_path, fake, lambda *_: resolved)
    action = make_action(p)
    result = board.create_held(action, title=p.title, body=p.body, assignee="implementer",
        workspace="dir:/repo", idempotency_key=p.idempotency_key)
    assert result.outcome == "conflict"
    assert action.target["kind"] == "accepted_active_tranche_piece_v2"
    assert not any(call[4] == "create" for call in fake.calls)


@pytest.mark.parametrize("resolver", [lambda *_: False, lambda *_: None])
def test_accepted_piece_verify_malformed_resolver_fails_closed(tmp_path, resolver):
    fake = FakeKanban(); fake.add("anchor-1")
    p = payload(); board = adapter(tmp_path, fake, resolver)
    result = board.verify_effect(make_action(p))
    assert result.outcome == "conflict"
    assert not any(call[4] == "create" for call in fake.calls)


def _native_piece_fixture(tmp_path, monkeypatch):
    """Disposable native board plus explicitly synthetic paid-planner provenance.

    This fixture demonstrates strict local-store authority only; it does not claim
    that a native planner executed. Native CLI effects are confined to tmp_path.
    """
    executable = os.environ.get("HERMES_M0_CLI")
    if not executable or not Path(executable).is_file():
        pytest.skip("native accepted-piece proof requires pinned HERMES_M0_CLI")
    executable = str(Path(executable).resolve())
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    board_id = "m4pieceproof"
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_KANBAN_BOARD=board_id)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_KANBAN_LOGS_ROOT", "HERMES_PROFILE", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        env.pop(key, None)
    monkeypatch.setenv("HERMES_M0_CLI", executable)
    def cli(*args):
        result = subprocess.run([executable, "kanban", "--board", board_id, *args], env=env,
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    cli("boards", "create", board_id)
    workspace = str(tmp_path / "workspace"); Path(workspace).mkdir()
    anchor = json.loads(cli("create", "anchor", "--assignee", "default", "--workspace", "dir:"+workspace, "--json").stdout)["id"]
    scope = {"board_id": board_id, "anchor_task_id": anchor}
    store = EvidenceStore.open(tmp_path / "evidence.sqlite", create_new=True); store.migrate()
    store.register_member(ManagedMember(board_id, anchor, anchor, "root", 0, (), "native-root"))
    req0, proposal, _ = __import__("tests.test_m4_active_tranche_materialization", fromlist=["fixture"]).fixture()
    req = dataclasses.replace(req0, board_id=board_id, anchor_id=anchor)
    proposal = dataclasses.replace(proposal, request_identity=req.identity)
    evidence = evidence_payload(req, proposal, planner_task_id="fixture-planner", planner_run_id="fixture-run",
        planner_session_id="fixture-session", planner_profile="paid-planner")
    store.register_member(ManagedMember(board_id, anchor, "fixture-planner", "planner", 0,
        ("__general_attempt__",), req.identity))
    release = OperationIntent("fixture-release", scope,
        {"task_id":"fixture-planner", "request_id":req.identity, "member_generation":0, "profile":"paid-planner"},
        "release", "synthetic-held", {"task_id":"fixture-planner"}, None, None, {}, "pending")
    event = {"event_id":"paid_capacity:fixture-release", "lineage_id":anchor+":__general_attempt__",
        "root_task_id":anchor, "finding_id":"__general_attempt__", "generation":0,
        "source_task_id":"fixture-planner", "source_kind":"native_operation",
        "native_source_id":"fixture-release", "count":1}
    store.reserve_paid_release(scope, release, event, policy_limit=1)
    marker_data = json.dumps({"action_key":release.key,"anchor_task_id":anchor,"board_id":board_id,"effect":"release"},sort_keys=True,separators=(",",":"))
    marker = "<!-- local-first-native:v1:sha256:"+hashlib.sha256(marker_data.encode()).hexdigest()+" -->"
    receipt = BoardSnapshot(native_task={"id":"fixture-planner","assignee":"paid-planner","status":"ready"},
        parents=(),runs=(),comments=({"body":"UNBLOCK: "+marker},),events=(),attachments=(),observed_at="fixture",digest="fixture")
    store.ack_effect(scope, release.key, readback=receipt.to_dict())
    run = {"id":"fixture-run","task_id":"fixture-planner","profile":"paid-planner","status":"running","started":True}
    store.bind_paid_release_to_native_run(scope, release.key, "fixture-planner", 0, "paid-planner",
        "fixture-run", "fixture-session", run)
    store.register_planning_request(scope, req, "fixture-planner", "paid-planner", request_id="fixture-request")
    store.record_plan(scope, evidence)
    store.record_accepted_plan(scope, proposal.plan.plan_id,
        route={"implementation_profile":"implementer", "workspace":workspace}, request_id="fixture-request")
    plan_id = proposal.plan.plan_id
    def resolver(bound_store):
        def plain(value):
            if isinstance(value, dict) or hasattr(value, "items"):
                return {str(k): plain(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(v) for v in value]
            return value
        def resolve(got_scope, key):
            token = bound_store.read_accepted_plan(got_scope, plan_id)
            ev = bound_store.read_plan(got_scope, plan_id)
            token_json = plain(token)
            evidence_json = plain(ev)
            req2, prop2 = reconstruct_evidence(evidence_json)
            material = first_active_tranche_materialization(evidence_json,
                ActiveTrancheRoute(token_json["route"]["implementation_profile"], token_json["route"]["workspace"]))
            built = accepted_active_tranche_create_payload(token_json, evidence_json, material.targets[0])
            assert built.idempotency_key == key
            return AcceptedDescription(built.title, built.body, plain(built.target), built.idempotency_key, evidence_json)
        return resolve
    def lock_assertion(_scope, _anchor):
        assert locked["held"]
    locked = {"held":False}
    def claim(got_scope, key):
        assert locked["held"]
        intent = OperationIntent(key, got_scope, {"task_id":anchor}, "create_held", "accepted-token:"+key,
            {"accepted_token_identity":token["acceptance_identity"]}, None, None, {}, "pending")
        store.reserve_operation(intent)
        store.begin_effect_attempt(got_scope, key)
        return True
    token = store.read_accepted_plan(scope, plan_id)
    store.close()
    return executable, env, board_id, anchor, scope, plan_id, claim, lock_assertion, resolver


@pytest.mark.parametrize("lost_response", [False, True], ids=["verified-create", "lost-response-restart-reconcile"])
def test_native_accepted_piece_create_is_store_authorized_parentless_and_durable(tmp_path, monkeypatch, lost_response):
    executable, env, board_id, anchor, scope, plan_id, _claim, _lock, _resolver = _native_piece_fixture(tmp_path, monkeypatch)
    store = EvidenceStore.open(tmp_path / "evidence.sqlite")
    resolver = _resolver(store)
    lock = {"held":False}
    def assert_locked(*_): assert lock["held"]
    def claim(sc, key):
        assert lock["held"]
        intent = OperationIntent(key, sc, {"task_id":anchor}, "create_held", "accepted-token:"+key,
            {"accepted_token_identity":store.read_accepted_plan(sc, plan_id)["acceptance_identity"]}, None, None, {}, "pending")
        store.reserve_operation(intent); store.begin_effect_attempt(sc, key)
        # Must be durable before native runner sees create.
        stored = store._operation(sc, key)
        assert stored.phase == "unknown" and stored.outcome == "ambiguous"
        return True
    real_runner = subprocess.run
    creates = []
    lose = {"next":lost_response}
    def runner(argv, **kwargs):
        if "create" in argv:
            op = store.read_scope(scope)["operations"]
            assert any(x.effect == "create_held" and x.phase == "unknown" for x in op)
            creates.append(tuple(argv))
        result = real_runner(argv, **kwargs)
        if "create" in argv and lose["next"]:
            lose["next"] = False
            return subprocess.CompletedProcess(argv, 9, "", "simulated lost response after native effect")
        return result
    board = HermesBoardAdapter(board=board_id, anchor_task_id=anchor, executable=executable, runner=runner,
        hermes_home=Path(env["HERMES_HOME"]), kanban_home=Path(env["HERMES_KANBAN_HOME"]),
        create_lock_assertion=assert_locked, claim_create_attempt=claim, accepted_piece_create_lookup=resolver)
    # Canonical payload is obtained only through the strict store-backed resolver.
    evidence = store.read_plan(scope, plan_id)
    def thaw(value):
        if isinstance(value, dict) or hasattr(value, "items"):
            return {str(k): thaw(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(v) for v in value]
        return value
    token = thaw(store.read_accepted_plan(scope, plan_id))
    req, _proposal = reconstruct_evidence(evidence)
    from local_first_orchestrator.planning_coordinator import accepted_active_tranche_create_payload
    mat = first_active_tranche_materialization(evidence, ActiveTrancheRoute(token["route"]["implementation_profile"], token["route"]["workspace"]))
    canonical = accepted_active_tranche_create_payload(token, json.loads(json.dumps(evidence)), mat.targets[0])
    action = Action(canonical.idempotency_key, scope, {**dict(canonical.target), "task_id":anchor,
        "anchor_task_id":anchor,"native_parent":False,"native_deps":[]}, "create_held", board.read_task(anchor).digest)
    lock["held"] = True
    try:
        result = board.create_held(action, title=canonical.title, body=canonical.body,
            assignee=canonical.target["assignee"], workspace=canonical.target["workspace"], idempotency_key=canonical.idempotency_key)
    finally: lock["held"] = False
    assert result.outcome == ("unknown" if lost_response else "verified"), result.details
    assert len(creates) == 1
    op = store._operation(scope, action.key)
    if lost_response:
        assert op.phase == "unknown" and op.outcome == "ambiguous"
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        resolver = _resolver(store)
        board = HermesBoardAdapter(board=board_id, anchor_task_id=anchor, executable=executable, runner=runner,
            hermes_home=Path(env["HERMES_HOME"]), kanban_home=Path(env["HERMES_KANBAN_HOME"]),
            create_lock_assertion=assert_locked, claim_create_attempt=claim, accepted_piece_create_lookup=resolver)
        lock["held"] = True
        try:
            snap = board.verify_effect(action)
        finally:
            lock["held"] = False
        assert snap.outcome == "no-op", snap.details
        assert len(creates) == 1
    else:
        snap = result
    store.observe_effect(scope, action.key, outcome="verified", readback=snap.readback, phase="applied")
    store.ack_effect(scope, action.key, readback=snap.readback)
    final = store._operation(scope, action.key)
    assert final.phase == "applied"
    actual = board.read_task(final.readback["native_task"]["id"])
    assert actual.native_task["status"] == "blocked"
    assert actual.native_task["assignee"] == "implementer"
    assert actual.native_task.get("workspace_kind") == "dir"
    assert actual.native_task.get("workspace_path") == token["route"]["workspace"]
    assert not actual.parents and not actual.runs
    assert canonical.body in actual.native_task["body"] and "local-first-create:" in actual.native_task["body"]
    assert len([x for x in store.read_scope(scope)["budget_events"] if x["event_id"].startswith("paid_capacity:")]) == 1
    store.close()
