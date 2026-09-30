"""First-edge adapter contract tests; no native task claims outside the characterized fixture."""
from __future__ import annotations

import copy
import json
from types import MappingProxyType
import subprocess
import time
from pathlib import Path

import pytest

from local_first_orchestrator.contracts import Action
from local_first_orchestrator.hermes_board import (
    HermesBoardAdapter, validate_accepted_first_link_transition,
)

SCOPE = {"board_id": "fixture-board", "anchor_task_id": "anchor-1"}


def _raw(task_id, *, children=(), parents=(), status="blocked", events=()):
    return {"task": {"id": task_id, "status": status, "title": task_id, "body": "held body",
                     "assignee": "worker", "workspace": "dir:/repo"},
            "parents": list(parents), "children": list(children), "events": list(events),
            "comments": [], "attachments": []}


def _edge_fixture():
    source, child = _raw("native-A"), _raw("native-B")
    linked = {"created_at": 100, "kind": "linked", "payload": {"parent": "native-A", "child": "native-B"}, "run_id": None}
    after_source, after_child = copy.deepcopy(source), copy.deepcopy(child)
    after_source["children"] = ["native-B"]
    after_child["parents"] = ["native-A"]
    after_child["events"] = [linked]
    return source, child, after_source, after_child


def test_pure_first_edge_transition_requires_exact_source_append_and_child_history():
    before_source, before_child, after_source, after_child = _edge_fixture()
    transition = validate_accepted_first_link_transition(
        before_source, before_child, after_source, after_child,
        source_id="native-A", child_id="native-B", command_started_seconds=99,
        command_ended_seconds=101,
    )
    assert transition.event["kind"] == "linked"
    for mutated in ("extra-child", "comment", "run"):
        changed = copy.deepcopy(after_child)
        if mutated == "extra-child": changed["children"] = ["native-C"]
        elif mutated == "comment": changed["comments"] = [{"body": "human"}]
        else: changed["events"][0]["run_id"] = "run-1"
        with pytest.raises(ValueError):
            validate_accepted_first_link_transition(
                before_source, before_child, after_source, changed,
                source_id="native-A", child_id="native-B", command_started_seconds=99,
                command_ended_seconds=101,
            )


def test_transition_rejects_timestamp_outside_command_window():
    before_source, before_child, after_source, after_child = _edge_fixture()
    with pytest.raises(ValueError):
        validate_accepted_first_link_transition(
            before_source, before_child, after_source, after_child,
            source_id="native-A", child_id="native-B", command_started_seconds=101,
            command_ended_seconds=102,
        )


class _RawCli:
    def __init__(self):
        self.cards = {"native-A": _raw("native-A"), "native-B": _raw("native-B")}
        self.calls = []
        self.raise_after_effect = False
        self.make_edge = True

    def __call__(self, argv, **kwargs):
        args = tuple(argv[4:]); self.calls.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.cards[args[1]]), "")
        if args[0] == "runs": return subprocess.CompletedProcess(argv, 0, "[]", "")
        if args[0] == "link":
            if self.make_edge:
                self.cards[args[1]]["children"].append(args[2])
                self.cards[args[2]]["parents"].append(args[1])
                self.cards[args[2]]["events"].append({"created_at": int(time.time()), "kind": "linked",
                    "payload": {"parent": args[1], "child": args[2]}, "run_id": None})
            if self.raise_after_effect:
                return subprocess.CompletedProcess(argv, 9, "", "ambiguous")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "unsupported")


def _claim_receipt(action):
    from local_first_orchestrator.contracts import OperationIntent
    operation = OperationIntent(action.key, action.scope, action.target, action.effect,
        action.expected_observed_identity, {}, None, None, {}, "unknown")
    return {"before_phase": "pending", "operation": operation.to_dict()}


def _adapter(tmp_path, cli, *, claim=None):
    exe = tmp_path / "hermes"; exe.write_text("fixture")
    frozen_raws = (copy.deepcopy(cli.cards["native-A"]), copy.deepcopy(cli.cards["native-B"]))
    from local_first_orchestrator.planning_coordinator import _canonical_digest
    from local_first_orchestrator.contracts import BoardSnapshot
    from datetime import datetime, timezone
    def snap(raw):
        return BoardSnapshot(native_task=raw["task"], parents=(), runs=(), comments=(),
            events=tuple(raw["events"]), attachments=(), observed_at=datetime.now(timezone.utc).isoformat(),
            digest=HermesBoardAdapter._digest({"native_task": raw["task"], "parents": [], "runs": [],
                "comments": [], "events": raw["events"], "attachments": []}))
    frozen_snapshots = tuple(snap(x) for x in frozen_raws)
    receipts = [{"ticket_id": t, "task_id": i, "association": "assoc-" + t,
        "operation_key": "create-" + t, "digest": s.digest, "readback": s.to_dict()}
        for t, i, s in (("TK-A", "native-A", frozen_snapshots[0]), ("TK-B", "native-B", frozen_snapshots[1]))]
    authority = {"kind": "accepted_active_tranche_native_link_v1", "acceptance_identity": "sha256:" + "a" * 64,
        "plan_id": "plan-1", "request_identity": "sha256:" + "b" * 64, "scope": dict(SCOPE),
        "route": {"implementation_profile": "worker", "workspace": "/repo"}, "root_task_id": "anchor-1",
        "tranche_ordinal": 0, "tranche_id": "tranche-0", "source_ticket_id": "TK-A", "target_ticket_id": "TK-B",
        "source_association": "assoc-TK-A", "target_association": "assoc-TK-B", "source_task_id": "native-A",
        "target_task_id": "native-B", "frozen_create_receipts": receipts, "read_only_first_edge_only": True}
    authority["operation_key"] = "native-link:" + _canonical_digest(
        {k: v for k, v in authority.items() if k != "read_only_first_edge_only"})
    key = authority["operation_key"]
    def resolver(scope, callback_key):
        a, b = frozen_raws
        return {"target": {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": "native-A",
                "child_task_id": "native-B", "operation_key": callback_key}, "authority": authority,
            "source_id": "native-A", "child_id": "native-B", "before_source": copy.deepcopy(a),
            "before_child": copy.deepcopy(b), "expected_source": {**copy.deepcopy(a), "children": ["native-B"]},
            "source_snapshot": frozen_snapshots[0].to_dict(), "child_snapshot": frozen_snapshots[1].to_dict(),
            "command_started_seconds": int(time.time()) - 1}
    board = HermesBoardAdapter(board="fixture-board", anchor_task_id="anchor-1", executable=str(exe), runner=cli,
        hermes_home=Path("/fixture/home"), kanban_home=Path("/fixture/kanban"),
        managed_member_lookup=lambda scope, task: task in {"native-A", "native-B"},
        create_lock_assertion=lambda *_: None, accepted_dependency_link_lookup=resolver, dependency_link_attempt_claim=claim)
    target = {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": "native-A",
        "child_task_id": "native-B", "operation_key": key}
    return board, Action(key, SCOPE, target, "link", frozen_snapshots[1].digest)


@pytest.fixture
def native_fixture(tmp_path):
    from tests.test_m4_planner_creation import native_fixture as _native_fixture
    return _native_fixture.__wrapped__(tmp_path)


def test_native_accepted_tranche_first_edge_is_claimed_and_readback_verified(tmp_path, native_fixture, monkeypatch):
    from local_first_orchestrator.contracts import Action
    from local_first_orchestrator.evidence_store import EvidenceStore
    from tests.test_m4_active_piece_preparation import _accepted_plan, _batch_proposal

    (board_id, anchor, workspace, adapter, membership, cli, controller, store, scope,
     accepted, request_id) = _accepted_plan(tmp_path, native_fixture, monkeypatch, proposal_factory=_batch_proposal)
    plan_id = accepted["plan_id"]
    try:
        prepared = controller.prepare_active_tranche(plan_id, request_id=request_id)
        assert prepared["outcome"] == "held", prepared
        assert prepared["completed"] == prepared["total"] == 2
        assert tuple(piece["ticket_id"] for piece in prepared["pieces"]) == ("TK-A", "TK-B")
        assert all(piece["outcome"] == "held" for piece in prepared["pieces"])
        source_auth = controller.active_piece_dependency_authority(plan_id, "TK-B", "TK-A", request_id=request_id)
        source_id, child_id = source_auth["source_task_id"], source_auth["target_task_id"]
        assert source_id != child_id and source_auth["read_only_first_edge_only"] is True
        state = store.read_scope(scope)
        budget_before = tuple(state["budget_events"])
        assert not [e for e in budget_before if e["event_id"].startswith("paid_capacity:")][1:]
        source_receipt = next(x for x in source_auth["frozen_create_receipts"] if x["ticket_id"] == "TK-A")
        child_receipt = next(x for x in source_auth["frozen_create_receipts"] if x["ticket_id"] == "TK-B")
        before_source, before_child = json.loads(cli("show", source_id, "--json").stdout), json.loads(cli("show", child_id, "--json").stdout)
        target = {"kind": "accepted_active_tranche_native_link_v1", "source_task_id": source_id,
                  "child_task_id": child_id, "operation_key": source_auth["operation_key"]}
        action = Action(source_auth["operation_key"], scope, target, "link", child_receipt["digest"])
        intent = controller._intent_for(action)
        event_not_before = int(time.time())
        store.reserve_operation(intent)

        calls = []
        original_invoke = adapter._invoke
        def counted(*args, **kwargs):
            if args and args[0] == "link":
                calls.append(tuple(args))
                op = next(item for item in store.read_scope(scope)["operations"] if item.key == action.key)
                assert op.phase == "unknown"
            return original_invoke(*args, **kwargs)
        monkeypatch.setattr(adapter, "_invoke", counted)

        def plain(value):
            if isinstance(value, (dict, MappingProxyType)): return {k: plain(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)): return [plain(v) for v in value]
            return value
        def resolver(callback_scope, key):
            assert dict(callback_scope) == dict(scope) and key == action.key
            controller._assert_lock()
            current = store.read_scope(scope)
            op = next(item for item in current["operations"] if item.key == key)
            assert op.effect == "link" and op.phase in {"pending", "unknown", "applied"} and plain(op.target) == target
            token = store.read_accepted_plan(scope, plan_id)
            assert token["acceptance_identity"] == source_auth["acceptance_identity"]
            assert store.read_plan(scope, plan_id)
            raw_source = json.loads(cli("show", source_id, "--json").stdout)
            raw_child = json.loads(cli("show", child_id, "--json").stdout)
            return {"target": target, "authority": plain(source_auth), "source_id": source_id, "child_id": child_id,
                "before_source": before_source, "before_child": before_child,
                "expected_source": {**before_source, "children": [child_id]},
                "source_snapshot": plain(source_receipt["readback"]), "child_snapshot": plain(child_receipt["readback"]),
                "command_started_seconds": event_not_before}
        adapter.accepted_dependency_link_lookup = resolver
        def assert_owned(callback_scope, callback_anchor):
            assert dict(callback_scope) == dict(scope) and callback_anchor == anchor
            controller._assert_lock()
        adapter.create_lock_assertion = assert_owned
        claims = []
        def claim(callback_scope, key):
            assert dict(callback_scope) == dict(scope) and key == action.key
            assert next(op for op in store.read_scope(scope)["operations"] if op.key == key).phase == "pending"
            store.begin_effect_attempt(callback_scope, key)
            claimed = next(item for item in store.read_scope(scope)["operations"] if item.key == key)
            assert claimed.phase == "unknown"
            claims.append(key)
            return {"before_phase": "pending", "operation": claimed.to_dict()}
        adapter.dependency_link_attempt_claim = claim
        with controller.lock:
            result = adapter.link(action, source_id, child_id)
        assert result.outcome == "verified", result.details
        assert len(calls) == len(claims) == 1
        assert tuple(calls[0]) == ("link", source_id, child_id)
        # Detach the adapter's original native readback before the store can observe it;
        # JSON round-tripping also gives immutable tuple-backed values their transport form.
        receipt = json.loads(json.dumps(plain(result.readback)))
        assert receipt["kind"] == "accepted_active_tranche_native_link_readback_v1"
        assert receipt["source"]["children"] == [child_id]
        assert receipt["child"]["parents"] == [source_id]
        assert receipt["child"]["events"][-1]["kind"] == "linked"
        assert receipt["child"]["events"][-1]["payload"] == {"parent": source_id, "child": child_id}
        assert receipt["child"]["events"][-1]["run_id"] is None
        assert receipt["time_window"]["started"] <= receipt["child"]["events"][-1]["created_at"] <= receipt["time_window"]["ended"]
        store.record_effect_observation(scope, action.key, outcome=result.outcome, details=result.details, readback=receipt)
        store.ack_effect(scope, action.key, readback=receipt, outcome=result.outcome)
        operation = next(op for op in store.read_scope(scope)["operations"] if op.key == action.key)
        stored_ack = plain(operation.readback)
        def first_difference(left, right, path="$"):
            if type(left) is not type(right): return path, left, right
            if isinstance(left, dict):
                if left.keys() != right.keys(): return path + ".<keys>", sorted(left), sorted(right)
                for key in left:
                    found = first_difference(left[key], right[key], f"{path}.{key}")
                    if found: return found
            elif isinstance(left, list):
                if len(left) != len(right): return path + ".<length>", len(left), len(right)
                for index, (a, b) in enumerate(zip(left, right)):
                    found = first_difference(a, b, f"{path}[{index}]")
                    if found: return found
            elif left != right: return path, left, right
            return None
        difference = first_difference(stored_ack, receipt)
        assert operation.phase == "applied" and difference is None, difference
        membership_before = tuple(store.read_scope(scope)["members"])
        budget_after = tuple(store.read_scope(scope)["budget_events"])
        assert budget_after == budget_before
        store.close()
        store = EvidenceStore.open(tmp_path / "evidence.sqlite")
        membership["store"] = store
        controller.store = store
        def reopened_resolver(callback_scope, key):
            assert dict(callback_scope) == dict(scope) and key == action.key
            op = next(item for item in store.read_scope(scope)["operations"] if item.key == key)
            assert op.phase == "applied" and plain(op.target) == target
            store.read_accepted_plan(scope, plan_id)
            store.read_plan(scope, plan_id)
            return {"authority": plain(source_auth), "target": target, "source_id": source_id, "child_id": child_id,
                "before_source": before_source, "before_child": before_child,
                "expected_source": {**before_source, "children": [child_id]},
                "source_snapshot": plain(source_receipt["readback"]), "child_snapshot": plain(child_receipt["readback"]),
                "command_started_seconds": event_not_before}
        adapter.accepted_dependency_link_lookup = reopened_resolver
        with controller.lock:
            verified = adapter.verify_effect(action)
        assert verified.outcome == "verified", verified.details
        assert len(calls) == 1
        reopened_ack = plain(next(op for op in store.read_scope(scope)["operations"] if op.key == action.key).readback)
        assert reopened_ack == stored_ack == receipt
        assert tuple(store.read_scope(scope)["members"]) == membership_before
        assert tuple(store.read_scope(scope)["budget_events"]) == budget_before
        assert len([m for m in store.read_scope(scope)["members"] if m.task_id == child_id]) == 1
    finally:
        try: store.close()
        except Exception: pass


def test_pending_first_edge_claims_once_and_verifies_exact_two_card_readback(tmp_path):
    cli, claims = _RawCli(), []
    board, action = _adapter(tmp_path, cli)
    board.dependency_link_attempt_claim = lambda scope, key: (claims.append((dict(scope), key)), _claim_receipt(action))[1]
    result = board.link(action, "native-A", "native-B")
    assert result.outcome == "verified", result.details
    assert len(claims) == 1
    assert result.readback["kind"] == "accepted_active_tranche_native_link_readback_v1"
    assert [c[0] for c in cli.calls].count("link") == 1


def test_native_error_after_effect_is_unknown_then_readonly_recovery(tmp_path):
    cli, claims = _RawCli(), []
    cli.raise_after_effect = True
    board, action = _adapter(tmp_path, cli)
    board.dependency_link_attempt_claim = lambda *_: (claims.append(1), _claim_receipt(action))[1]
    assert board.link(action, "native-A", "native-B").outcome == "unknown"
    cli.raise_after_effect = False
    result = board.verify_effect(action)
    assert result.outcome == "verified", result.details
    assert len(claims) == 1
    assert [c[0] for c in cli.calls].count("link") == 1


def test_missing_edge_reconciliation_never_resends_or_claims(tmp_path):
    cli, claims = _RawCli(), []
    cli.make_edge = False
    board, action = _adapter(tmp_path, cli)
    board.dependency_link_attempt_claim = lambda *_: (claims.append(1), _claim_receipt(action))[1]
    # A pending call attempts the one command; absent edge is a contradiction.
    assert board.link(action, "native-A", "native-B").outcome == "conflict"
    assert len(claims) == 1
    assert [c[0] for c in cli.calls].count("link") == 1
    assert board.verify_effect(action).outcome in {"conflict", "unknown"}
    assert [c[0] for c in cli.calls].count("link") == 1


def test_missing_durable_claim_callback_is_zero_effect_unsupported(tmp_path):
    cli = _RawCli()
    board, action = _adapter(tmp_path, cli)
    result = board.link(action, "native-A", "native-B")
    assert result.outcome == "unsupported"
    assert not any(call[0] == "link" for call in cli.calls)


@pytest.mark.parametrize("receipt", [None, False, {}, {"before_phase": "unknown", "operation": {}},
    {"before_phase": "pending", "operation": {}},
])
def test_invalid_durable_claim_receipt_blocks_native_send(tmp_path, receipt):
    cli = _RawCli()
    board, action = _adapter(tmp_path, cli, claim=lambda *_: receipt)
    result = board.link(action, "native-A", "native-B")
    assert result.outcome == "unknown"
    assert not any(call[0] == "link" for call in cli.calls)


def test_claim_rejects_nested_hostile_target_without_invoking_hooks(tmp_path):
    class Hostile(dict):
        calls = 0
        def items(self):
            type(self).calls += 1
            raise AssertionError("items hook invoked")
    cli = _RawCli()
    board, action = _adapter(tmp_path, cli)
    receipt = _claim_receipt(action)
    receipt["operation"]["target"] = Hostile(receipt["operation"]["target"])
    board.dependency_link_attempt_claim = lambda *_: receipt
    result = board.link(action, "native-A", "native-B")
    assert result.outcome == "unknown"
    assert Hostile.calls == 0
    assert not any(call[0] == "link" for call in cli.calls)


def _assert_bad_wrapper(tmp_path, mutate):
    cli, claims = _RawCli(), []
    board, action = _adapter(tmp_path, cli)
    resolver = board.accepted_dependency_link_lookup
    def wrapped(scope, key):
        value = resolver(scope, key)
        mutate(value)
        return value
    board.accepted_dependency_link_lookup = wrapped
    board.dependency_link_attempt_claim = lambda *_: claims.append(1)
    result = board.link(action, "native-A", "native-B")
    assert result.outcome != "verified", result
    assert claims == []
    assert not any(call[0] == "link" for call in cli.calls)


def test_wrapper_rejects_wrong_scope_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"].update(scope={"board_id": "other", "anchor_task_id": "anchor-1"}))


def test_wrapper_rejects_wrong_key_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"].update(operation_key="native-link:" + "0" * 64))


def test_wrapper_rejects_authority_child_id_mismatch_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"].update(target_task_id="native-C"))


def test_wrapper_rejects_oversized_utf8_identifier_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"].update(plan_id="é" * 2048))


def test_wrapper_rejects_control_scalar_identifier_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"].update(plan_id="plan-\x01"))


def test_wrapper_rejects_wrong_frozen_digest_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value["authority"]["frozen_create_receipts"][1].update(digest="sha256:" + "0" * 64))


def test_wrapper_rejects_raw_source_canonical_digest_mismatch_before_claim_or_link(tmp_path):
    def mutate(value):
        value["before_source"]["task"]["title"] = "forged"
    _assert_bad_wrapper(tmp_path, mutate)


def test_wrapper_rejects_unknown_field_before_claim_or_link(tmp_path):
    _assert_bad_wrapper(tmp_path, lambda value: value.update(extra="not closed"))


def test_wrapper_rejects_oversized_raw_proof_before_claim_or_link(tmp_path):
    def mutate(value):
        value["before_source"]["task"]["body"] = "x" * (1024 * 1024 + 1)
    _assert_bad_wrapper(tmp_path, mutate)


def test_wrapper_rejects_hostile_dict_subclass_without_invoking_hooks(tmp_path):
    class Hostile(dict):
        calls = 0
        def items(self):
            type(self).calls += 1
            raise AssertionError("items hook invoked")
        def __iter__(self):
            type(self).calls += 1
            raise AssertionError("iter hook invoked")
        def keys(self):
            type(self).calls += 1
            raise AssertionError("keys hook invoked")
    cli, claims = _RawCli(), []
    board, action = _adapter(tmp_path, cli)
    board.accepted_dependency_link_lookup = lambda *_: Hostile()
    board.dependency_link_attempt_claim = lambda *_: claims.append(1)
    result = board.link(action, "native-A", "native-B")
    assert result.outcome != "verified", result
    assert Hostile.calls == 0
    assert claims == []
    assert not any(call[0] == "link" for call in cli.calls)


