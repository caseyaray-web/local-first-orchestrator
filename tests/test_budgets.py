from __future__ import annotations

import pytest
import threading

from local_first_orchestrator.budgets import (
    BudgetPolicy,
    GENERAL_ATTEMPT,
    WORKFLOW_REPAIRS,
    admit_repair_operation,
    classify_run_start,
    explain_exhaustion,
    permit_action,
    record_once,
    remaining,
)
from local_first_orchestrator.contracts import ConflictError, ManagedMember, OperationIntent
from local_first_orchestrator.evidence_store import EvidenceStore


SCOPE = {"board_id": "board-a", "anchor_task_id": "anchor-a"}
LIMITS = BudgetPolicy(
    implementation_attempts=2,
    review_corrections=1,
    infrastructure_retries=1,
    workflow_repairs=1,
    paid_capacity=1,
)


@pytest.fixture
def store(tmp_path):
    with EvidenceStore.open(tmp_path / "evidence.sqlite3", create_new=True) as opened:
        opened.migrate()
        opened.register_member(
            ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1")
        )
        opened.register_member(
            ManagedMember("board-a", "anchor-a", "implementation-2", "implementation", 1, (), "work-1")
        )
        yield opened


def event(*, event_id="implementation_attempts:run-1", source_task_id="implementation-1", generation=0, native_source_id="run-1", finding_id=GENERAL_ATTEMPT):
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
        "run": {"status": "queued"},
    }


def repair_operation(*, key="repair:finding-1:1"):
    return OperationIntent(
        key=key,
        scope=SCOPE,
        target={"task_id": "implementation-1"},
        effect="request_review",
        expected_observed_identity="observed-1",
        before_evidence={"digest": "before-1"},
        outcome=None,
        readback=None,
        retry={"stable_marker": key},
        phase="pending",
    )


def repair_event(*, key="repair:finding-1:1", finding_id=GENERAL_ATTEMPT, generation=0):
    return {
        "event_id": f"{WORKFLOW_REPAIRS}:{key}",
        "lineage_id": f"anchor-a:{finding_id}",
        "root_task_id": "anchor-a",
        "finding_id": finding_id,
        "generation": generation,
        "source_task_id": "implementation-1",
        "source_kind": "native_operation",
        "native_source_id": key,
        "count": 1,
    }


def test_policy_requires_explicit_finite_limit_for_every_category():
    with pytest.raises(TypeError):
        BudgetPolicy(implementation_attempts=1, review_corrections=1, infrastructure_retries=1, workflow_repairs=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="non-negative"):
        BudgetPolicy(implementation_attempts=-1, review_corrections=1, infrastructure_retries=1, workflow_repairs=1, paid_capacity=1)


def test_replacement_and_reopen_do_not_reset_shared_root_finding_budget(store, tmp_path):
    assert record_once(store, SCOPE, event(), policy=LIMITS) is True
    assert record_once(store, SCOPE, event(event_id="implementation_attempts:run-2", source_task_id="implementation-2", generation=1, native_source_id="run-2"), policy=LIMITS) is True
    assert remaining(LIMITS, store, SCOPE, "implementation_attempts") == 0

    database = store.path
    store.close()
    reopened = EvidenceStore.open(database)
    try:
        assert remaining(LIMITS, reopened, SCOPE, "implementation_attempts") == 0
        assert not permit_action(LIMITS, reopened, SCOPE, "implementation_attempts")
    finally:
        reopened.close()


def test_duplicate_native_source_is_not_counted_again_across_replacement(store):
    first = event()
    assert record_once(store, SCOPE, first, policy=LIMITS) is True
    duplicate = event(source_task_id="implementation-2", generation=1)
    assert record_once(store, SCOPE, duplicate, policy=LIMITS) is False
    assert remaining(LIMITS, store, SCOPE, "implementation_attempts") == 1


def test_admission_category_matching_is_literal_not_sql_like(store):
    foreign = event(event_id="implementationXattempts:foreign", native_source_id="foreign")
    store.record_budget_event(SCOPE, {key: value for key, value in foreign.items() if key != "run"})
    policy = BudgetPolicy(1, 1, 1, 1, 1)
    assert remaining(policy, store, SCOPE, "implementation_attempts") == 1
    assert record_once(store, SCOPE, event(), policy=policy) is True
    assert remaining(policy, store, SCOPE, "implementation_attempts") == 0


def test_wrong_root_or_unregistered_finding_is_rejected(store):
    with pytest.raises(ConflictError, match="root task"):
        record_once(store, SCOPE, {**event(), "root_task_id": "other-anchor", "lineage_id": f"other-anchor:{GENERAL_ATTEMPT}"}, policy=LIMITS)
    with pytest.raises(ConflictError, match="finding"):
        record_once(store, SCOPE, event(finding_id="finding-not-associated"), policy=LIMITS)


def test_unknown_active_run_counts_conservatively_while_known_pre_start_failure_does_not(store):
    assert classify_run_start({"status": "queued"}) == "unknown_active"
    assert record_once(store, SCOPE, {**event(), "run": {"status": "queued"}}, policy=LIMITS) is True
    assert record_once(store, SCOPE, {**event(event_id="implementation_attempts:run-2", native_source_id="run-2"), "run": {"status": "failed", "started_at": None}}, policy=LIMITS) is False
    assert remaining(LIMITS, store, SCOPE, "implementation_attempts") == 1


def test_paid_capacity_requires_explicit_authorization_even_when_budget_remains(store):
    assert not permit_action(LIMITS, store, SCOPE, "paid_capacity")
    assert permit_action(LIMITS, store, SCOPE, "paid_capacity", paid_authorized=True)


def test_zero_limit_is_exhausted_and_has_actionable_explanation(store):
    policy = BudgetPolicy(implementation_attempts=0, review_corrections=1, infrastructure_retries=1, workflow_repairs=1, paid_capacity=1)
    assert remaining(policy, store, SCOPE, "implementation_attempts") == 0
    assert not permit_action(policy, store, SCOPE, "implementation_attempts")
    explanation = explain_exhaustion(policy, store, SCOPE, "implementation_attempts")
    assert "anchor-a" in explanation
    assert "implementation_attempts" in explanation
    assert "hold" in explanation


def test_record_once_requires_explicit_policy_and_native_run_observation(store):
    with pytest.raises(TypeError):
        record_once(store, SCOPE, event())
    with pytest.raises(ValueError, match="run observation"):
        record_once(store, SCOPE, {key: value for key, value in event().items() if key != "run"}, policy=LIMITS)


def test_atomic_limit_one_admits_only_one_distinct_native_source(tmp_path):
    database = tmp_path / "atomic.sqlite3"
    policy = BudgetPolicy(implementation_attempts=1, review_corrections=1, infrastructure_retries=1, workflow_repairs=1, paid_capacity=1)
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
    barrier = threading.Barrier(2)
    results: list[object] = []

    def admit(native_source_id: str) -> None:
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                results.append(record_once(concurrent, SCOPE, {**event(event_id=f"implementation_attempts:{native_source_id}", native_source_id=native_source_id), "run": {"status": "queued"}}, policy=policy))
            except BaseException as error:
                results.append(error)

    threads = [threading.Thread(target=admit, args=(source,)) for source in ("run-a", "run-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results.count(True) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1


def test_unknown_run_reconciliation_without_a_trusted_native_reader_never_credits(store):
    charged = {**event(), "run": {"status": "queued"}}
    assert record_once(store, SCOPE, charged, policy=LIMITS)
    assert remaining(LIMITS, store, SCOPE, "implementation_attempts") == 1
    with pytest.raises(ConflictError, match="trusted native"):
        store.reconcile_unknown_budget_run(SCOPE, source_task_id="implementation-1", generation=0, finding_id=GENERAL_ATTEMPT, native_run_id="run-1")
    assert remaining(LIMITS, store, SCOPE, "implementation_attempts") == 1


def test_trusted_native_reread_releases_once_and_persists_hashed_typed_proof(tmp_path):
    database = tmp_path / "trusted.sqlite3"
    native_runs = {("implementation-1", "run-1"): {"id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None}}
    reader_calls = []

    def read_native_run(scope, task_id, run_id):
        reader_calls.append((dict(scope), task_id, run_id))
        return native_runs[(task_id, run_id)]

    with EvidenceStore.open(database, create_new=True, read_native_run=read_native_run) as trusted:
        trusted.migrate()
        trusted.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
        assert record_once(trusted, SCOPE, event(), policy=LIMITS)
        assert trusted.reconcile_unknown_budget_run(SCOPE, source_task_id="implementation-1", generation=0, finding_id=GENERAL_ATTEMPT, native_run_id="run-1") is True
        assert reader_calls == [(SCOPE, "implementation-1", "run-1")]
        state = trusted.read_scope(SCOPE)
        proof = state["budget_reconciliations"][1]
        assert proof["evidence_kind"] == "pre_start_proof"
        assert proof["evidence"]["native_run"] == native_runs[("implementation-1", "run-1")]
        assert proof["evidence"]["native_run_sha256"].startswith("sha256:")
        assert state["budget_net"] == ({"root_task_id": "anchor-a", "finding_id": GENERAL_ATTEMPT, "category": "implementation_attempts", "charged": 1, "reconciled": 1, "net": 0},)
        assert remaining(LIMITS, trusted, SCOPE, "implementation_attempts") == 2
        assert trusted.reconcile_unknown_budget_run(SCOPE, source_task_id="implementation-1", generation=0, finding_id=GENERAL_ATTEMPT, native_run_id="run-1") is False
    with EvidenceStore.open(database, read_native_run=read_native_run) as reopened:
        assert reopened.read_scope(SCOPE)["budget_net"][0]["net"] == 0
        assert remaining(LIMITS, reopened, SCOPE, "implementation_attempts") == 2


@pytest.mark.parametrize("native_run", [
    {"id": "wrong-run", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
    {"id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None, "started": True},
    {"id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": "2020-01-01"},
])
def test_trusted_native_reread_requires_exact_pre_start_run(tmp_path, native_run):
    database = tmp_path / "rejected-proof.sqlite3"
    with EvidenceStore.open(database, create_new=True, read_native_run=lambda *_: native_run) as trusted:
        trusted.migrate()
        trusted.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
        assert record_once(trusted, SCOPE, event(), policy=LIMITS)
        with pytest.raises(ConflictError, match="pre-start|identity"):
            trusted.reconcile_unknown_budget_run(SCOPE, source_task_id="implementation-1", generation=0, finding_id=GENERAL_ATTEMPT, native_run_id="run-1")
        assert remaining(LIMITS, trusted, SCOPE, "implementation_attempts") == 1


@pytest.mark.parametrize(
    "native_run",
    [
        {"run_id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
        {"id": "run-1", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
        {"id": "run-1", "task_id": "wrong-task", "board_id": "board-a", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
        {"id": "run-1", "task_id": "implementation-1", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
        {"id": "run-1", "task_id": "implementation-1", "board_id": "wrong-board", "anchor_task_id": "anchor-a", "status": "failed", "started_at": None},
        {"id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "status": "failed", "started_at": None},
        {"id": "run-1", "task_id": "implementation-1", "board_id": "board-a", "anchor_task_id": "wrong-anchor", "status": "failed", "started_at": None},
    ],
)
def test_trusted_native_reread_requires_exact_task_board_and_anchor_provenance(tmp_path, native_run):
    database = tmp_path / "rejected-provenance.sqlite3"
    with EvidenceStore.open(database, create_new=True, read_native_run=lambda *_: native_run) as trusted:
        trusted.migrate()
        trusted.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
        assert record_once(trusted, SCOPE, event(), policy=LIMITS)
        with pytest.raises(ConflictError, match="provenance|identity"):
            trusted.reconcile_unknown_budget_run(SCOPE, source_task_id="implementation-1", generation=0, finding_id=GENERAL_ATTEMPT, native_run_id="run-1")
        assert remaining(LIMITS, trusted, SCOPE, "implementation_attempts") == 1


def test_per_finding_admission_caps_and_reconciled_charge_do_not_cross_findings(tmp_path):
    database = tmp_path / "per-finding.sqlite3"
    policy = BudgetPolicy(implementation_attempts=1, review_corrections=1, infrastructure_retries=1, workflow_repairs=1, paid_capacity=1)
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("f1", "f2"), "work-1"))
        assert record_once(opened, SCOPE, event(event_id="implementation_attempts:f1-1", native_source_id="f1-1", finding_id="f1"), policy=policy)
        assert record_once(opened, SCOPE, event(event_id="implementation_attempts:f2-1", native_source_id="f2-1", finding_id="f2"), policy=policy)
        with pytest.raises(ConflictError, match="exhausted"):
            record_once(opened, SCOPE, event(event_id="implementation_attempts:f1-2", native_source_id="f1-2", finding_id="f1"), policy=policy)
    with EvidenceStore.open(database) as reopened:
        assert remaining(policy, reopened, SCOPE, "implementation_attempts", finding_id="f1") == 0
        assert remaining(policy, reopened, SCOPE, "implementation_attempts", finding_id="f2") == 0


def test_budgeted_repair_operation_reserves_and_charges_once_across_restart(tmp_path):
    database = tmp_path / "repair-operation.sqlite3"
    policy = BudgetPolicy(1, 1, 1, 1, 1)
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, ("finding-1",), "work-1"))
        reserved = admit_repair_operation(policy, opened, SCOPE, repair_operation(), repair_event(finding_id="finding-1"))
        assert reserved.phase == "pending"
        assert remaining(policy, opened, SCOPE, WORKFLOW_REPAIRS, finding_id="finding-1") == 0
    with EvidenceStore.open(database) as reopened:
        assert admit_repair_operation(policy, reopened, SCOPE, repair_operation(), repair_event(finding_id="finding-1")) == reserved
        assert len(reopened.read_scope(SCOPE)["budget_events"]) == 1
        assert remaining(policy, reopened, SCOPE, WORKFLOW_REPAIRS, finding_id="finding-1") == 0


def test_existing_budgeted_repair_admission_is_idempotent_at_cap_but_new_repair_fails(store):
    policy = BudgetPolicy(1, 1, 1, 1, 1)
    original = admit_repair_operation(policy, store, SCOPE, repair_operation(), repair_event())

    assert admit_repair_operation(policy, store, SCOPE, repair_operation(), repair_event()) == original
    with pytest.raises(ConflictError, match="exhausted"):
        admit_repair_operation(
            policy, store, SCOPE, repair_operation(key="repair:finding-1:2"), repair_event(key="repair:finding-1:2"),
        )
    assert len(store.read_scope(SCOPE)["operations"]) == 1


def test_atomic_repair_operation_admission_allows_only_one_new_key_at_cap(tmp_path):
    database = tmp_path / "atomic-repair-operation.sqlite3"
    policy = BudgetPolicy(1, 1, 1, 1, 1)
    with EvidenceStore.open(database, create_new=True) as opened:
        opened.migrate()
        opened.register_member(ManagedMember("board-a", "anchor-a", "implementation-1", "implementation", 0, (), "work-1"))
    barrier = threading.Barrier(2)
    results: list[object] = []

    def admit(key: str) -> None:
        with EvidenceStore.open(database) as concurrent:
            barrier.wait()
            try:
                results.append(admit_repair_operation(policy, concurrent, SCOPE, repair_operation(key=key), repair_event(key=key)))
            except BaseException as error:
                results.append(error)

    threads = [threading.Thread(target=admit, args=(key,)) for key in ("repair:general:1", "repair:general:2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sum(isinstance(result, OperationIntent) for result in results) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    with EvidenceStore.open(database) as reopened:
        assert len(reopened.read_scope(SCOPE)["operations"]) == 1
        assert len(reopened.read_scope(SCOPE)["budget_events"]) == 1


def test_exhausted_repair_budget_does_not_block_unbudgeted_operator_containment(store):
    policy = BudgetPolicy(1, 1, 1, 0, 1)
    with pytest.raises(ConflictError, match="exhausted"):
        admit_repair_operation(policy, store, SCOPE, repair_operation(), repair_event())

    containment = OperationIntent(
        **{**repair_operation(key="hold:implementation-1:observed-1").to_dict(), "effect": "hold_task"}
    )
    assert store.reserve_operation(containment) == containment
    assert store.read_scope(SCOPE)["budget_events"] == ()
