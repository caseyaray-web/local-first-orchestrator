from __future__ import annotations

import sqlite3
import threading

import pytest

from local_first_orchestrator.contracts import (
    CandidateIdentity,
    ConflictError,
    ManagedMember,
    OperationIntent,
    PauseIntent,
)
from local_first_orchestrator.evidence_store import EvidenceStore, SchemaError
from local_first_orchestrator.operator_controls import plan_resume, reconcile_operator_edits


SCOPE = {"board_id": "board-a", "anchor_task_id": "anchor-a"}
OTHER_SCOPE = {"board_id": "board-b", "anchor_task_id": "anchor-a"}


@pytest.fixture
def store(tmp_path):
    database = tmp_path / "plugin-evidence.sqlite3"
    opened = EvidenceStore.open(database, create_new=True)
    opened.migrate()
    try:
        yield opened, database
    finally:
        opened.close()


def candidate(*, head_sha="head-1"):
    return CandidateIdentity(
        repository_identity="repo-1",
        worktree="/worktree",
        base_sha="base-1",
        head_sha=head_sha,
        content_identity="content-1",
        diff_identity="diff-1",
        originating_run_id="run-1",
        contract_hash="contract-1",
    )


def review(*, review_id="review-1", candidate_identity=None, run_id="review-run-1"):
    bound_candidate = candidate() if candidate_identity is None else candidate_identity
    checks = [{"check_id": "tests", "outcome": "passed", "evidence": "pytest: 35 passed"}]
    return {
        "review_id": review_id,
        "candidate_identity": bound_candidate.to_dict(),
        "reviewer_role": "local",
        "native_review": {"task_id": "review-task-1", "run_id": run_id, "session_id": "session-1", "profile": "local"},
        "checks": checks,
        "checks_identity": "e45fca8b0449fa833dcc3791a80f30980c9c7839a3712e36a2310bc5f39e25cd",
        "verdict": "approved",
        "criterion_evidence": [{"criterion_id": "tests", "outcome": "pass", "evidence": "pytest: 35 passed"}],
        "findings": [],
    }


def budget_event(*, event_id="run-1:attempt", source_task_id="implementation-1", finding_id="finding-1", generation=0, native_source_id="run-1"):
    return {
        "event_id": event_id,
        "lineage_id": f"anchor-a:{finding_id}",
        "root_task_id": "anchor-a",
        "finding_id": finding_id,
        "generation": generation,
        "source_task_id": source_task_id,
        "source_kind": "native_run",
        "native_source_id": native_source_id,
        "count": 1,
    }


def operation(*, key="hold-1", scope=SCOPE):
    return OperationIntent(
        key=key,
        scope=scope,
        target={"task_id": "task-1"},
        effect="hold_task",
        expected_observed_identity="observation-1",
        before_evidence={"marker": "before-1"},
        outcome=None,
        readback=None,
        retry={"stable_marker": "hold-1"},
        phase="pending",
    )


def test_no_shadow_ticket_lifecycle_schema(store):
    opened, _ = store
    tables = {
        row[0]
        for row in opened.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {
        row[1]
        for table in tables
        for row in opened.connection.execute(f'PRAGMA table_info("{table}")')
    }
    assert tables == {
        "schema_metadata",
        "managed_members",
        "candidates",
        "review_evidence",
        "operation_intents",
        "budget_events",
        "budget_reconciliation_evidence",
        "effect_observations",
        "operator_intents",
    }
    assert not ({"status", "lane", "task_state", "board_snapshot", "dependencies", "runs"} & columns)


def test_pause_survives_restart(store):
    opened, database = store
    pause = PauseIntent(SCOPE, "operator", 2, True, True)
    assert opened.set_operator_intent(pause) == pause
    opened.close()

    reopened = EvidenceStore.open(database)
    try:
        assert reopened.read_scope(SCOPE)["operator_intent"] == pause
    finally:
        reopened.close()


def test_unknown_effect_preserved_and_never_returned_as_replayable(store):
    opened, _ = store
    reserved = opened.reserve_operation(operation())
    unknown = opened.observe_effect(
        SCOPE,
        reserved.key,
        outcome="ambiguous",
        readback=None,
        phase="unknown",
    )
    assert unknown.phase == "unknown"
    assert opened.pending_operations(SCOPE) == (unknown,)
    with pytest.raises(ConflictError, match="unknown"):
        opened.reserve_operation(operation())
    with pytest.raises(ConflictError, match="unknown"):
        opened.observe_effect(SCOPE, reserved.key, outcome=None, readback=None, phase="pending")
    assert opened.ack_effect(SCOPE, reserved.key, readback={"stable_marker": "hold-1"}).phase == "applied"


def create_operation(*, key="create-1", scope=SCOPE):
    return OperationIntent(
        key=key,
        scope=scope,
        target={"anchor_task_id": scope["anchor_task_id"]},
        effect="create_held",
        expected_observed_identity="observation-1",
        before_evidence={"marker": "before-1"},
        outcome=None,
        readback=None,
        retry={"stable_marker": key},
        phase="pending",
    )


@pytest.mark.parametrize("effect", ["hold_task", "release", "stop_run", "create_held"])
def test_begin_effect_attempt_durably_marks_each_supported_pending_effect_unknown_before_io(store, effect):
    opened, _ = store
    intent = create_operation() if effect == "create_held" else OperationIntent(
        **{**operation(key=f"{effect}-1").to_dict(), "effect": effect}
    )
    opened.reserve_operation(intent)

    begun = opened.begin_effect_attempt(SCOPE, intent.key)

    assert begun.phase == "unknown"
    assert begun.outcome == "ambiguous"
    assert opened.read_scope(SCOPE)["operations"] == (begun,)
    with pytest.raises(ConflictError, match="unknown"):
        opened.begin_effect_attempt(SCOPE, intent.key)


def test_begin_effect_attempt_rejects_unsupported_reserved_intent(store):
    opened, _ = store
    opened.reserve_operation(OperationIntent(**{**operation().to_dict(), "effect": "publish_external"}))

    with pytest.raises(ConflictError, match="unsupported"):
        opened.begin_effect_attempt(SCOPE, "hold-1")
    with pytest.raises(KeyError, match="not reserved"):
        opened.begin_effect_attempt(SCOPE, "missing")


def test_begin_effect_attempt_survives_restart_and_prohibits_resend(store):
    opened, database = store
    opened.reserve_operation(operation())
    assert opened.begin_effect_attempt(SCOPE, "hold-1").phase == "unknown"
    opened.close()

    with EvidenceStore.open(database) as reopened:
        with pytest.raises(ConflictError, match="unknown"):
            reopened.begin_effect_attempt(SCOPE, "hold-1")


def test_non_success_action_result_is_append_only_truthful_evidence(store):
    opened, _ = store
    opened.reserve_operation(operation())

    saved = opened.record_effect_observation(
        SCOPE, "hold-1", outcome="partial", details="native hold accepted but readback incomplete",
        readback={"task_id": "task-1", "state": "holding"},
    )

    assert saved == {"operation_key": "hold-1", "outcome": "partial", "details": "native hold accepted but readback incomplete", "readback": {"task_id": "task-1", "state": "holding"}}
    assert opened.record_effect_observation(
        SCOPE, "hold-1", outcome="partial", details="native hold accepted but readback incomplete",
        readback={"task_id": "task-1", "state": "holding"},
    ) == saved
    assert opened.read_scope(SCOPE)["effect_observations"] == (saved,)
    with pytest.raises(ValueError, match="supported action result"):
        opened.record_effect_observation(SCOPE, "hold-1", outcome="not-an-action-result", details="invalid", readback=None)
    assert opened.read_scope(SCOPE)["effect_observations"] == (saved,)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        opened.connection.execute("UPDATE effect_observations SET observation_json = '{}' ")


def test_read_scope_bounds_effect_observations(store):
    opened, _ = store
    opened.reserve_operation(operation())
    for number in range(65):
        opened.record_effect_observation(
            SCOPE, "hold-1", outcome="unsupported", details=f"unsupported-{number}", readback=None,
        )

    observations = opened.read_scope(SCOPE)["effect_observations"]
    assert len(observations) == 64
    assert all(record["outcome"] == "unsupported" for record in observations)


def test_duplicate_operation_is_idempotent_but_changed_payload_conflicts(store):
    opened, _ = store
    first = opened.reserve_operation(operation())
    assert opened.reserve_operation(operation()) == first
    changed = OperationIntent(
        **{**operation().to_dict(), "effect": "unhold_task"}
    )
    with pytest.raises(ConflictError, match="operation key"):
        opened.reserve_operation(changed)


def test_scopes_are_board_and_anchor_and_do_not_collide(store):
    opened, _ = store
    opened.register_member(
        ManagedMember("board-a", "anchor-a", "task-1", "implementation", 0, (), "piece-1")
    )
    opened.register_member(
        ManagedMember("board-b", "anchor-a", "task-1", "implementation", 0, (), "piece-1")
    )
    assert len(opened.read_scope(SCOPE)["members"]) == 1
    assert len(opened.read_scope(OTHER_SCOPE)["members"]) == 1


def test_candidate_and_review_are_bound_to_exact_identities(store):
    opened, _ = store
    saved = opened.record_candidate(SCOPE, candidate())
    assert opened.record_candidate(SCOPE, candidate()) == saved
    with pytest.raises(ConflictError, match="candidate identity"):
        opened.record_candidate(SCOPE, candidate(head_sha="head-2"))

    evidence = review()
    assert opened.record_review(SCOPE, candidate(), evidence) == evidence
    assert opened.record_review(SCOPE, candidate(), evidence) == evidence
    changed = {
        **evidence,
        "verdict": "changes_requested",
        "criterion_evidence": [{"criterion_id": "tests", "outcome": "fail", "evidence": "pytest: failing"}],
        "findings": [{"finding_id": "finding-1", "criterion_id": "tests", "severity": "major", "summary": "test failure"}],
    }
    with pytest.raises(ConflictError, match="review identity"):
        opened.record_review(SCOPE, candidate(), changed)


def test_budget_lineage_deduplicates_across_replacement_members(store):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
    event = budget_event()
    assert opened.record_budget_event(SCOPE, event) == event
    assert opened.record_budget_event(SCOPE, event) == event
    with pytest.raises(ConflictError, match="budget event"):
        opened.record_budget_event(SCOPE, {**event, "count": 2})


def test_migrate_refuses_existing_database_without_plugin_schema(tmp_path):
    database = tmp_path / "unrelated.sqlite3"
    sqlite3.connect(database).execute("CREATE TABLE unrelated (value TEXT)").connection.close()
    opened = EvidenceStore.open(database)
    try:
        with pytest.raises(SchemaError, match="recognized"):
            opened.migrate()
    finally:
        opened.close()


def test_migrate_valid_v1_to_v2_preserves_budget_evidence(tmp_path):
    database = tmp_path / "v1.sqlite3"
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
        saved = budget_event()
        opened.record_budget_event(SCOPE, saved)
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE budget_reconciliation_evidence")
    connection.execute("DROP TABLE effect_observations")
    connection.execute("UPDATE schema_metadata SET value='1' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    with EvidenceStore.open(database) as upgraded:
        upgraded.migrate()
        assert upgraded.read_scope(SCOPE)["budget_events"] == (saved,)
        assert upgraded.connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "3"


def test_migrate_valid_v2_to_v3_preserves_operation_evidence(tmp_path):
    database = tmp_path / "v2.sqlite3"
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        saved = opened.reserve_operation(operation())
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE effect_observations")
    connection.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with EvidenceStore.open(database) as upgraded:
        upgraded.migrate()
        assert upgraded.read_scope(SCOPE)["operations"] == (saved,)
        assert upgraded.connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "3"


def test_migrate_rejects_malformed_v2_before_any_write(tmp_path):
    database = tmp_path / "malformed-v2.sqlite3"
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE effect_observations")
    connection.execute("CREATE TRIGGER surplus_v2_trigger BEFORE INSERT ON budget_events BEGIN SELECT 1; END")
    connection.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with EvidenceStore.open(database) as opened:
        with pytest.raises(SchemaError, match="trigger"):
            opened.migrate()
        assert opened.connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "2"
        assert opened.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='effect_observations'").fetchone() is None


def test_migrate_rejects_v1_with_malformed_review_foreign_key_before_any_write(tmp_path):
    database = tmp_path / "malformed-v1.sqlite3"
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE review_evidence RENAME TO old_review_evidence")
    connection.execute("CREATE TABLE review_evidence (board_id TEXT NOT NULL, anchor_task_id TEXT NOT NULL, candidate_content_identity TEXT NOT NULL, review_id TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, review_id))")
    connection.execute("DROP TABLE old_review_evidence")
    connection.execute("DROP TABLE budget_reconciliation_evidence")
    connection.execute("DROP TABLE effect_observations")
    connection.execute("UPDATE schema_metadata SET value='1' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with EvidenceStore.open(database) as opened:
        with pytest.raises(SchemaError, match="foreign"):
            opened.migrate()
        assert opened.connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "1"
        assert opened.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='budget_reconciliation_evidence'").fetchone() is None


def test_migrate_rejects_v1_with_surplus_index_before_any_write(tmp_path):
    database = tmp_path / "surplus-index-v1.sqlite3"
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE budget_reconciliation_evidence")
    connection.execute("DROP TABLE effect_observations")
    connection.execute("CREATE INDEX surplus_v1_index ON budget_events (event_id)")
    connection.execute("UPDATE schema_metadata SET value='1' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    with EvidenceStore.open(database) as opened:
        with pytest.raises(SchemaError, match="indexes"):
            opened.migrate()
        assert opened.connection.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0] == "1"
        assert opened.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='budget_reconciliation_evidence'").fetchone() is None


def test_reconciliation_evidence_is_append_only_and_trigger_schema_is_checked_after_reopen(store):
    opened, database = store
    opened.connection.execute("INSERT INTO budget_reconciliation_evidence VALUES (?, ?, ?, ?, ?)", ("board-a", "anchor-a", "event-1", "fixture", "{}"))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        opened.connection.execute("UPDATE budget_reconciliation_evidence SET evidence_json='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        opened.connection.execute("DELETE FROM budget_reconciliation_evidence")
    opened.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER budget_reconciliation_evidence_immutable_delete")
    connection.commit()
    connection.close()
    with EvidenceStore.open(database) as reopened:
        with pytest.raises(SchemaError, match="trigger"):
            reopened.read_scope(SCOPE)


def test_open_missing_configured_database_fails_closed_without_creating_a_file(tmp_path):
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(SchemaError, match="does not exist"):
        EvidenceStore.open(database)
    assert not database.exists()


def test_open_create_new_bootstraps_once_and_never_replaces_an_existing_database(tmp_path):
    database = tmp_path / "bootstrap.sqlite3"
    opened = EvidenceStore.open(database, create_new=True)
    try:
        opened.migrate()
    finally:
        opened.close()
    with pytest.raises(SchemaError, match="already exists"):
        EvidenceStore.open(database, create_new=True)


def test_open_create_new_rejects_a_dangling_symlink_without_creating_its_target(tmp_path):
    database = tmp_path / "configured.sqlite3"
    target = tmp_path / "target.sqlite3"
    database.symlink_to(target)
    with pytest.raises(SchemaError, match="regular file"):
        EvidenceStore.open(database, create_new=True)
    assert not target.exists()


def test_open_rejects_a_non_private_parent_directory(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    with pytest.raises(SchemaError, match="private"):
        EvidenceStore.open(parent / "configured.sqlite3", create_new=True)


def test_record_review_rejects_a_candidate_binding_that_does_not_match_every_identity_field(store):
    opened, _ = store
    bound_candidate = candidate()
    evidence = review(review_id="review-binding-1", candidate_identity=bound_candidate)
    assert opened.record_review(SCOPE, bound_candidate, evidence) == evidence
    with pytest.raises(ConflictError, match="candidate binding"):
        opened.record_review(
            SCOPE,
            bound_candidate,
            {**evidence, "review_id": "review-binding-2", "candidate_identity": {**bound_candidate.to_dict(), "contract_hash": "wrong-contract"}},
        )
    assert opened.read_scope(SCOPE)["reviews"] == (evidence,)


def test_reserving_original_intent_after_ack_returns_stored_applied_effect(store):
    opened, _ = store
    original = operation()
    opened.reserve_operation(original)
    applied = opened.ack_effect(SCOPE, original.key, readback={"stable_marker": "hold-1"})
    assert opened.reserve_operation(original) == applied


@pytest.mark.parametrize("table", ["review_evidence", "operation_intents"])
def test_schema_validation_rejects_malformed_same_name_version_table(tmp_path, table):
    database = tmp_path / f"malformed-{table}.sqlite3"
    opened = EvidenceStore.open(database, create_new=True)
    opened.migrate()
    opened.close()
    connection = sqlite3.connect(database)
    connection.execute(f"DROP TABLE {table}")
    connection.execute(f"CREATE TABLE {table} (value TEXT)")
    connection.commit()
    connection.close()

    reopened = EvidenceStore.open(database)
    try:
        with pytest.raises(SchemaError, match="schema"):
            reopened.read_scope(SCOPE)
    finally:
        reopened.close()


def test_budget_event_attribution_survives_replacement_member_and_new_ticket(store):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
    event = budget_event()
    opened.record_budget_event(SCOPE, event)
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-2", "implementation", 1, ("finding-1",), "work-1"))
    replacement_event = budget_event(event_id="run-2:attempt", source_task_id="implementation-2", generation=1, native_source_id="run-2")
    opened.record_budget_event(SCOPE, replacement_event)
    state = opened.read_scope(SCOPE)
    assert state["budget_events"] == (event, replacement_event)
    assert sum(item["count"] for item in state["budget_events"]) == 2


@pytest.mark.parametrize("field, value", [("reviewer_role", "implementation"), ("checks_identity", "wrong"), ("checks", []), ("criterion_evidence", []), ("findings", [{"finding_id": "unexpected"}])])
def test_review_requires_canonical_checks_and_normalized_verdict_evidence(field, value, store):
    opened, _ = store
    with pytest.raises(ValueError):
        opened.record_review(SCOPE, candidate(), {**review(), field: value})


def test_review_requires_an_independent_native_run_without_claiming_adapter_verification(store):
    opened, _ = store
    with pytest.raises(ConflictError, match="run"):
        opened.record_review(SCOPE, candidate(), review(run_id="run-1"))


@pytest.mark.parametrize("mutation", [
    {"root_task_id": "wrong-root"},
    {"lineage_id": "wrong-lineage"},
    {"source_task_id": "unknown-member"},
    {"generation": 1},
    {"finding_id": "wrong-finding", "lineage_id": "anchor-a:wrong-finding"},
])
def test_budget_event_is_bound_to_registered_member_generation_finding_and_deterministic_lineage(store, mutation):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
    with pytest.raises((ValueError, ConflictError)):
        opened.record_budget_event(SCOPE, {**budget_event(), **mutation})


def test_budget_event_deduplicates_immutable_native_source_across_replacement_members(store):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-2", "implementation", 1, ("finding-1",), "work-1"))
    opened.record_budget_event(SCOPE, budget_event())
    with pytest.raises(ConflictError, match="native source"):
        opened.record_budget_event(SCOPE, budget_event(event_id="other-event", source_task_id="implementation-2", generation=1))


def test_budget_event_uses_explicit_general_attempt_sentinel_without_finding_membership(store):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
    event = budget_event(finding_id="__general_attempt__")
    assert opened.record_budget_event(SCOPE, event) == event


def test_duplicate_ack_is_idempotent_but_changed_ack_conflicts(store):
    opened, _ = store
    opened.reserve_operation(operation())
    applied = opened.ack_effect(SCOPE, "hold-1", readback={"stable_marker": "hold-1"})
    assert opened.ack_effect(SCOPE, "hold-1", readback={"stable_marker": "hold-1"}) == applied
    with pytest.raises(ConflictError, match="applied"):
        opened.ack_effect(SCOPE, "hold-1", readback={"stable_marker": "different"})


@pytest.mark.parametrize(
    "outcome, readback",
    [(None, {"stable_marker": "hold-1"}), ("ambiguous", {"stable_marker": "hold-1"}), ("verified", None), ("verified", {})],
)
def test_store_rejects_incomplete_applied_observations(store, outcome, readback):
    opened, _ = store
    opened.reserve_operation(operation())
    with pytest.raises(ValueError, match="applied"):
        opened.observe_effect(SCOPE, "hold-1", outcome=outcome, readback=readback, phase="applied")
    assert opened.pending_operations(SCOPE) == (operation(),)


def test_schema_validation_rejects_missing_required_not_null_and_metadata_cardinality(tmp_path):
    database = tmp_path / "weakened.sqlite3"
    opened = EvidenceStore.open(database, create_new=True)
    opened.migrate()
    opened.close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE operation_intents RENAME TO old_operation_intents")
    connection.execute("CREATE TABLE operation_intents (board_id TEXT, anchor_task_id TEXT NOT NULL, operation_key TEXT NOT NULL, intent_json TEXT NOT NULL, PRIMARY KEY (board_id, anchor_task_id, operation_key))")
    connection.execute("DROP TABLE old_operation_intents")
    connection.execute("INSERT INTO schema_metadata(key, value) VALUES ('extra', '1')")
    connection.commit()
    connection.close()
    reopened = EvidenceStore.open(database)
    try:
        with pytest.raises(SchemaError, match="schema"):
            reopened.read_scope(SCOPE)
    finally:
        reopened.close()


def test_malformed_persisted_json_is_a_schema_error(store):
    opened, _ = store
    opened.reserve_operation(operation())
    opened.connection.execute(
        "UPDATE operation_intents SET intent_json = ? WHERE board_id = ? AND anchor_task_id = ? AND operation_key = ?",
        ("{not-json", "board-a", "anchor-a", "hold-1"),
    )
    opened.connection.commit()
    with pytest.raises(SchemaError, match="persisted"):
        opened.read_scope(SCOPE)


def test_record_review_conflict_does_not_register_new_candidate(store):
    opened, _ = store
    first = candidate()
    opened.record_review(SCOPE, first, review(review_id="shared-review"))
    second = CandidateIdentity(**{**first.to_dict(), "content_identity": "content-2", "head_sha": "head-2"})
    conflicting = review(review_id="shared-review", candidate_identity=second)
    with pytest.raises(ConflictError, match="review identity"):
        opened.record_review(SCOPE, second, conflicting)
    assert opened.read_scope(SCOPE)["candidates"] == (first,)


def test_concurrent_native_source_charges_only_once(store):
    opened, database = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
    barrier = threading.Barrier(2)
    successes: list[object] = []
    failures: list[BaseException] = []

    def charge(event_id):
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                successes.append(concurrent.record_budget_event(SCOPE, budget_event(event_id=event_id)))
            except BaseException as error:
                failures.append(error)

    threads = [threading.Thread(target=charge, args=(event_id,)) for event_id in ("charge-a", "charge-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)
    assert len(opened.read_scope(SCOPE)["budget_events"]) == 1


def test_concurrent_conflicting_acks_do_not_overwrite_each_other(store):
    opened, database = store
    opened.reserve_operation(operation())
    barrier = threading.Barrier(2)
    successes: list[OperationIntent] = []
    failures: list[BaseException] = []

    def acknowledge(marker):
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                successes.append(concurrent.ack_effect(SCOPE, "hold-1", readback={"stable_marker": marker}))
            except BaseException as error:
                failures.append(error)

    threads = [threading.Thread(target=acknowledge, args=(marker,)) for marker in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)
    assert opened._operation(SCOPE, "hold-1") == successes[0]


def test_budget_event_rejects_zero_count_before_persisting(store):
    opened, _ = store
    opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))

    with pytest.raises(ValueError, match="positive"):
        opened.record_budget_event(SCOPE, {**budget_event(), "count": 0})

    assert opened.read_scope(SCOPE)["budget_events"] == ()


@pytest.mark.parametrize(
    "outcome, readback",
    [
        ("verified", None),
        ("no-op", None),
        ("ambiguous", {"stable_marker": "hold-1"}),
    ],
)
def test_unknown_effect_requires_ambiguous_outcome_without_verified_readback(store, outcome, readback):
    opened, _ = store
    opened.reserve_operation(operation())

    with pytest.raises(ValueError, match="unknown"):
        opened.observe_effect(SCOPE, "hold-1", outcome=outcome, readback=readback, phase="unknown")

    assert opened.pending_operations(SCOPE) == (operation(),)


@pytest.mark.parametrize(
    "record",
    [
        lambda opened: opened.register_member(ManagedMember("board-a", "anchor-a", "task-1", "implementation", 0, (), "work-1")),
        lambda opened: opened.record_candidate(SCOPE, candidate()),
        lambda opened: opened.reserve_operation(operation()),
    ],
    ids=["member", "candidate", "operation"],
)
def test_concurrent_identical_first_write_is_idempotent(store, record):
    _, database = store
    barrier = threading.Barrier(2)
    successes: list[object] = []
    failures: list[BaseException] = []

    def write_identical():
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                successes.append(record(concurrent))
            except BaseException as error:
                failures.append(error)

    threads = [threading.Thread(target=write_identical) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 2
    assert failures == []


def test_concurrent_distinct_generation_one_operator_intents_do_not_last_write_wins(store):
    opened, database = store
    opened.set_operator_intent(PauseIntent(SCOPE, "operator", 0, False, False))
    intents = (
        PauseIntent(SCOPE, "operator", 1, True, False),
        PauseIntent(SCOPE, "operator", 1, False, True),
    )
    barrier = threading.Barrier(2)
    successes: list[PauseIntent] = []
    failures: list[BaseException] = []

    def set_intent(intent):
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                successes.append(concurrent.set_operator_intent(intent))
            except BaseException as error:
                failures.append(error)

    threads = [threading.Thread(target=set_intent, args=(intent,)) for intent in intents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)
    assert opened.read_scope(SCOPE)["operator_intent"] == successes[0]


def test_store_rejects_first_or_unauthorized_inactive_pause_clear_without_altering_state(store):
    opened, _ = store
    first_clear = PauseIntent(SCOPE, "operator", 0, False, False, active=False)

    with pytest.raises(ConflictError, match="active"):
        opened.set_operator_intent(first_clear, authorized_clear=True)
    assert opened.read_scope(SCOPE)["operator_intent"] is None

    active = PauseIntent(SCOPE, "operator", 6, False, False)
    opened.set_operator_intent(active)
    clear = PauseIntent(SCOPE, "operator", 7, False, False, active=False)
    with pytest.raises(ConflictError, match="authorization"):
        opened.set_operator_intent(clear)
    assert opened.read_scope(SCOPE)["operator_intent"] == active


def test_store_persists_only_authorized_plan_resume_clear_and_reopens_idempotently(store):
    opened, database = store
    active = PauseIntent(SCOPE, "operator", 6, False, False)
    opened.set_operator_intent(active)
    decision = plan_resume(
        SCOPE, pause_intent=active,
        reconciliation=reconcile_operator_edits(SCOPE, (), (), (), ()),
        operator_authorized_resume=True,
    )
    assert decision.intent is not None

    assert opened.set_operator_intent(
        decision.intent, authorized_clear=True, resume_decision=decision,
    ) == decision.intent
    assert opened.set_operator_intent(
        decision.intent, authorized_clear=True, resume_decision=decision,
    ) == decision.intent
    opened.close()

    with EvidenceStore.open(database) as reopened:
        assert reopened.read_scope(SCOPE)["operator_intent"] == decision.intent


def test_store_rejects_stale_or_unproven_pause_clear_without_altering_active_intent(store):
    opened, _ = store
    active = PauseIntent(SCOPE, "operator", 6, False, False)
    opened.set_operator_intent(active)
    stale = PauseIntent(SCOPE, "operator", 8, False, False, active=False)
    stale_decision = plan_resume(
        SCOPE, pause_intent=active,
        reconciliation=reconcile_operator_edits(SCOPE, (), (), (), ()),
        operator_authorized_resume=True,
    )

    with pytest.raises(ConflictError, match="generation"):
        opened.set_operator_intent(stale, authorized_clear=True, resume_decision=stale_decision)
    with pytest.raises(ConflictError, match="decision"):
        opened.set_operator_intent(
            PauseIntent(SCOPE, "operator", 7, False, False, active=False),
            authorized_clear=True,
        )
    assert opened.read_scope(SCOPE)["operator_intent"] == active
