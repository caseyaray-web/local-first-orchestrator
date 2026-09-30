import hashlib
import json

import pytest

from local_first_orchestrator.contracts import BoardSnapshot, ConflictError, ManagedMember, OperationIntent
from local_first_orchestrator.evidence_store import EvidenceStore, SchemaError

SCOPE = {"board_id": "board", "anchor_task_id": "root"}


def setup_store(tmp_path):
    store = EvidenceStore.open(tmp_path / "store.sqlite", create_new=True)
    store.migrate()
    store.register_member(ManagedMember("board", "root", "planner", "planner", 3, ("__general_attempt__",), "request-1"))
    return store


def release_intent(key="release-1"):
    return OperationIntent(key, SCOPE, {"task_id": "planner", "request_id": "request-1", "member_generation": 3, "profile": "paid"}, "release", "release-identity", {"task_id": "planner"}, None, None, {"immutable": True}, "pending")


def release_event(key="release-1"):
    return {"event_id": f"paid_capacity:{key}", "lineage_id": "root:__general_attempt__", "root_task_id": "root", "finding_id": "__general_attempt__", "generation": 3, "source_task_id": "planner", "source_kind": "native_operation", "native_source_id": key, "count": 1}


def receipt(key="release-1", *, status="ready", extra=None):
    marker_data = json.dumps({"action_key": key, "anchor_task_id": "root", "board_id": "board", "effect": "release"}, sort_keys=True, separators=(",", ":"))
    marker = "<!-- local-first-native:v1:sha256:" + hashlib.sha256(marker_data.encode()).hexdigest() + " -->"
    value = BoardSnapshot(native_task={"id": "planner", "assignee": "paid", "status": status}, parents=(), runs=(), comments=({"body": f"UNBLOCK: {marker}"},), events=(), attachments=(), observed_at="now", digest="digest").to_dict()
    if extra:
        value.update(extra)
    return value


def acknowledge(store, key="release-1"):
    return store.ack_effect(SCOPE, key, readback=receipt(key))


def test_paid_release_reservation_is_durable_idempotent_and_pre_effect(tmp_path):
    store = setup_store(tmp_path)
    intent = release_intent()
    assert store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1) == intent
    assert store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=0) == intent
    with pytest.raises(ConflictError):
        store.reserve_paid_release(SCOPE, release_intent("release-2"), release_event("release-2"), policy_limit=1)
    assert len(store.read_scope(SCOPE)["budget_events"]) == 1
    store.close()
    reopened = EvidenceStore.open(tmp_path / "store.sqlite")
    reopened.migrate()
    assert len(reopened.read_scope(SCOPE)["budget_events"]) == 1


def test_binding_requires_applied_exact_reservation_and_persists_immutable_binding(tmp_path):
    store = setup_store(tmp_path)
    intent = release_intent()
    store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1)
    acknowledge(store, intent.key)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    bound = store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-1", "session-1", run)
    assert bound["run_id"] == "run-1"
    assert store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-1", "session-1", run) == bound
    with pytest.raises(ConflictError):
        store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-2", "session-2", {**run, "id": "run-2"})
    assert len(store.read_scope(SCOPE)["budget_events"]) == 1
    with pytest.raises(Exception):
        store.connection.execute("UPDATE paid_release_run_bindings SET native_run_id='mutated'")
    with pytest.raises(Exception):
        store.connection.execute("DELETE FROM paid_release_run_bindings")


@pytest.mark.parametrize("change", [
    {"id": "other-run"}, {"task_id": "other-task"}, {"profile": "other-profile"},
    {"worker_session_id": "other-session"},
])
def test_binding_rejects_changed_native_identity_without_partial_writes(tmp_path, change):
    store = setup_store(tmp_path)
    intent = release_intent()
    store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1)
    acknowledge(store, intent.key)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    with pytest.raises(ConflictError):
        store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-1", "session-1", {**run, **change})
    assert len(store.read_scope(SCOPE)["budget_events"]) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM paid_release_run_bindings").fetchone()[0] == 0


def test_binding_requires_applied_release_and_refuses_prestart_without_refund(tmp_path):
    store = setup_store(tmp_path)
    intent = release_intent()
    store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    with pytest.raises(ConflictError):
        store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-1", "session-1", run)
    acknowledge(store, intent.key)
    with pytest.raises(ConflictError):
        store.bind_paid_release_to_native_run(SCOPE, intent.key, "planner", 3, "paid", "run-1", "session-1", {**run, "status": "failed", "started": False, "started_at": None})
    assert len(store.read_scope(SCOPE)["budget_events"]) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM paid_release_run_bindings").fetchone()[0] == 0


def test_reserve_paid_release_cap_zero_and_malformed_inputs_are_write_free(tmp_path):
    store = setup_store(tmp_path)
    with pytest.raises(ConflictError):
        store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=0)
    assert store.read_scope(SCOPE)["budget_events"] == ()
    assert store.read_scope(SCOPE)["operations"] == ()
    with pytest.raises(ValueError):
        store.reserve_paid_release(SCOPE, release_intent(), {**release_event(), "count": True}, policy_limit=1)
    assert store.read_scope(SCOPE)["budget_events"] == ()
    assert store.read_scope(SCOPE)["operations"] == ()
    with pytest.raises(ValueError):
        store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=True)
    assert store.read_scope(SCOPE)["operations"] == ()
    store.close()


@pytest.mark.parametrize("alias", ["paid_capacity_alias:release-1", "paid_capacity:release-1:alias", "paid_capacity:"])
def test_reservation_rejects_event_alias_without_writes(tmp_path, alias):
    store = setup_store(tmp_path)
    with pytest.raises(ConflictError):
        store.reserve_paid_release(SCOPE, release_intent(), {**release_event(), "event_id": alias}, policy_limit=1)
    assert store.read_scope(SCOPE)["budget_events"] == ()
    assert store.read_scope(SCOPE)["operations"] == ()


@pytest.mark.parametrize("receipt_value", [
    {"status": "released"}, {"status": "blocked"}, {"extra": {"unexpected": True}},
])
def test_binding_requires_canonical_full_snapshot_receipt(tmp_path, receipt_value):
    store = setup_store(tmp_path)
    store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=1)
    evidence = receipt(status=receipt_value.get("status", "ready"), extra=receipt_value.get("extra"))
    store.ack_effect(SCOPE, "release-1", readback=evidence)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    with pytest.raises(ConflictError):
        store.bind_paid_release_to_native_run(SCOPE, "release-1", "planner", 3, "paid", "run-1", "session-1", run)
    assert store.connection.execute("SELECT COUNT(*) FROM paid_release_run_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("generation", 99), ("root_task_id", "elsewhere"), ("finding_id", "f"), ("source_task_id", "elsewhere"), ("lineage_id", "bad"), ("count", 2), ("native_source_id", "other"), ("source_kind", "native_run")])
def test_read_binding_rejects_corrupt_persisted_charge(tmp_path, field, value):
    store = setup_store(tmp_path)
    store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=1)
    acknowledge(store)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    store.bind_paid_release_to_native_run(SCOPE, "release-1", "planner", 3, "paid", "run-1", "session-1", run)
    row = store.connection.execute("SELECT event_json FROM budget_events WHERE event_id='paid_capacity:release-1'").fetchone()
    event = json.loads(row[0])
    event[field] = value
    store.connection.execute("UPDATE budget_events SET event_json=? WHERE event_id='paid_capacity:release-1'", (json.dumps(event, sort_keys=True, separators=(",", ":")),))
    with pytest.raises(SchemaError):
        store.read_paid_release_run_binding(SCOPE, "release-1")


@pytest.mark.parametrize("field,value", [
    ("native_session_id", ""), ("request_identity", ""), ("native_profile", ""),
    ("run_classification", "bogus"), ("native_run_sha256", "bad"), ("native_run_id", "bad id"),
    ("member_generation", True),
])
def test_read_binding_rejects_corrupt_binding_columns(tmp_path, field, value):
    store = setup_store(tmp_path)
    intent = release_intent()
    store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1)
    acknowledge(store)
    run = {"id": "run-1", "task_id": "planner", "profile": "paid", "status": "running", "started": True}
    store.bind_paid_release_to_native_run(SCOPE, "release-1", "planner", 3, "paid", "run-1", "session-1", run)
    store.connection.execute("DROP TRIGGER paid_release_run_bindings_immutable_update")
    store.connection.execute(f"UPDATE paid_release_run_bindings SET {field}=? WHERE release_operation_key='release-1'", (value,))
    with pytest.raises(SchemaError):
        store.read_paid_release_run_binding(SCOPE, "release-1")


@pytest.mark.parametrize("corruption", ["badjson", "negative_count", "boolean_count", "wrong_category", "wrong_source", "wrong_lineage"])
def test_reservation_fails_closed_on_corrupt_existing_budget_without_writes(tmp_path, corruption):
    store = setup_store(tmp_path)
    event = {"event_id": "paid_capacity:prior", "lineage_id": "root:__general_attempt__", "root_task_id": "root", "finding_id": "__general_attempt__", "generation": 3, "source_task_id": "planner", "source_kind": "native_run", "native_source_id": "run-prior", "count": 1}
    store.record_budget_event(SCOPE, event)
    if corruption == "badjson":
        store.connection.execute("UPDATE budget_events SET event_json='{' WHERE event_id=?", (event["event_id"],))
    else:
        changed = dict(event)
        changed.update({"negative_count": {"count": -1}, "boolean_count": {"count": True}, "wrong_category": {"event_id": "other:prior"}, "wrong_source": {"native_source_id": "different"}, "wrong_lineage": {"lineage_id": "wrong"}}[corruption])
        store.connection.execute("UPDATE budget_events SET event_json=? WHERE event_id=?", (json.dumps(changed, sort_keys=True, separators=(",", ":")), event["event_id"]))
    snapshot = tuple(tuple(row) for row in store.connection.execute("SELECT event_id,source_kind,native_source_id,event_json FROM budget_events ORDER BY event_id"))
    store.connection.commit()
    with pytest.raises(SchemaError):
        store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=5)
    assert tuple(tuple(row) for row in store.connection.execute("SELECT event_id,source_kind,native_source_id,event_json FROM budget_events ORDER BY event_id")) == snapshot
    assert store.connection.execute("SELECT COUNT(*) FROM operation_intents").fetchone()[0] == 0


@pytest.mark.parametrize("corruption", ["retargeted_finding", "multiunit_paid", "unknown_category"])
def test_paid_admission_refuses_consistent_but_invalid_existing_charge_without_writes(tmp_path, corruption):
    store = setup_store(tmp_path)
    if corruption == "retargeted_finding":
        store.register_member(ManagedMember("board", "root", "planner-2", "planner", 3, ("finding-1",), "request-2"))
        event = {**release_event("prior"), "finding_id": "finding-1", "lineage_id": "root:finding-1", "source_task_id": "planner-2", "native_source_id": "run-prior", "event_id": "paid_capacity:run-prior", "source_kind": "native_run"}
    else:
        event = {**release_event("prior"), "native_source_id": "run-prior", "event_id": ("unknown_category" if corruption == "unknown_category" else "paid_capacity") + ":run-prior", "source_kind": "native_run"}
        if corruption == "multiunit_paid":
            event["count"] = 2
    store.record_budget_event(SCOPE, event)
    row_id = event["event_id"]
    if corruption == "retargeted_finding":
        stored = dict(event)
        stored.update(event_id="paid_capacity:run-prior", native_source_id="run-prior")
    else:
        stored = event
    store.connection.execute("UPDATE budget_events SET event_id=?,source_kind=?,native_source_id=?,event_json=? WHERE event_id=?", (stored["event_id"], stored["source_kind"], stored["native_source_id"], json.dumps(stored, sort_keys=True, separators=(",", ":")), row_id))
    snapshot = tuple(tuple(row) for row in store.connection.execute("SELECT event_id,source_kind,native_source_id,event_json FROM budget_events ORDER BY event_id"))
    store.connection.commit()
    with pytest.raises(SchemaError):
        store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=5)
    assert tuple(tuple(row) for row in store.connection.execute("SELECT event_id,source_kind,native_source_id,event_json FROM budget_events ORDER BY event_id")) == snapshot
    assert store.connection.execute("SELECT COUNT(*) FROM operation_intents").fetchone()[0] == 0


def test_valid_native_run_paid_charge_consumes_exactly_one_capacity(tmp_path):
    store = setup_store(tmp_path)
    event = {**release_event("run-prior"), "event_id": "paid_capacity:run-prior", "native_source_id": "run-prior", "source_kind": "native_run"}
    store.record_budget_event(SCOPE, event)
    with pytest.raises(ConflictError):
        store.reserve_paid_release(SCOPE, release_intent(), release_event(), policy_limit=1)
    assert len(store.read_scope(SCOPE)["budget_events"]) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM operation_intents").fetchone()[0] == 0


def test_unknown_paid_release_exact_retry_returns_unknown_without_new_charge(tmp_path):
    store = setup_store(tmp_path)
    intent = release_intent()
    store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=1)
    unknown = store.begin_effect_attempt(SCOPE, intent.key)
    retried = store.reserve_paid_release(SCOPE, intent, release_event(), policy_limit=0)
    assert retried == unknown
    assert retried.phase == "unknown"
    assert store.connection.execute("SELECT COUNT(*) FROM budget_events").fetchone()[0] == 1
