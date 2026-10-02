"""Concurrency proof for the public paid-correction materialization path."""
from __future__ import annotations

import threading
import time

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.contracts import CandidateIdentity
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import InstanceLockError, instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_m4_public_core_flow import (
    _complete_integrated_serial_core,
    _review,
    _snapshot,
)


def test_m5_two_concurrent_paid_correction_coordinators_create_one_held_card_and_charge_once(tmp_path, monkeypatch):
    """Independent SQLite connections race one paid correction under one singleton lock."""
    seed, store, board, cards, runs, git, _repo, base, head = _complete_integrated_serial_core(tmp_path, monkeypatch)
    database = store.path
    try:
        seed.combined_check_runner = lambda context: {
            "head_sha": context["head_sha"],
            "checks": [{"check_id": "smoke", "command": "python -c pass", "exit_code": 0,
                        "output_sha256": "sha256:" + context["head_sha"]}],
        }
        request = seed.prepare_paid_integrated_review("plan-1", git_adapter=git)
        assert seed.release_paid_integrated_review("plan-1", git_adapter=git)["outcome"] == "released"
        paid_task = request["review_task_id"]
        runs[paid_task] = ({"id": "paid-concurrent", "task_id": paid_task, "profile": "paid",
                            "status": "completed", "metadata": {"worker_session_id": "paid-concurrent-session"}},)
        cards[paid_task] = _snapshot(cards[paid_task], status="done", assignee="paid")
        candidate = CandidateIdentity.from_dict(next(
            op for op in store.read_scope(seed.scope)["operations"] if op.key == request["review_request_key"]
        ).readback["candidate"])
        approved = _review("paid-concurrent-approved", candidate, role="paid", task_id=paid_task,
                           run_id="paid-concurrent", session="paid-concurrent-session")
        assert seed.submit_paid_integrated_review("plan-1", approved, git_adapter=git)["outcome"] == "approved"
        runs[paid_task] = ({**runs[paid_task][0], "status": "running"},)
        cards[paid_task] = _snapshot(cards[paid_task], status="blocked", assignee="paid")
        changes = _review(
            "paid-concurrent-changes", candidate, role="paid", task_id=paid_task,
            run_id="paid-concurrent", session="paid-concurrent-session", verdict="changes_requested",
            findings=[{"finding_id": "finding-concurrent", "criterion_id": "AC-1",
                       "severity": "major", "summary": "shared concurrent fixture"}],
        )
        assert seed.submit_paid_integrated_review("plan-1", changes, git_adapter=git)["outcome"] == "changes_requested"
    finally:
        store.close()

    create_calls: list[str] = []
    create_guard = threading.Lock()
    real_create = board.create_held

    def counted_create(action, **kwargs):
        with create_guard:
            create_calls.append(action.key)
        return real_create(action, **kwargs)

    board.create_held = counted_create
    start = threading.Barrier(2)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def fresh_coordinator_attempt() -> None:
        connection = EvidenceStore.open(database)
        try:
            accepted_reader = connection.read_accepted_plan
            connection.read_accepted_plan = lambda scope, plan_id: {
                **accepted_reader(scope, plan_id), "base_sha": base,
            }
            controller = Coordinator(
                seed.scope,
                board=board,
                store=connection,
                lock=instance_lock(tmp_path / "shared-paid-correction.lock"),
                budget_policy=BudgetPolicy(5, 5, 5, 5, 5),
                configured_roles=dict(seed.configured_roles),
            )
            start.wait(timeout=10)
            for _ in range(100):
                try:
                    result = controller.prepare_paid_correction(
                        "plan-1", "paid-concurrent-changes", git_adapter=git,
                    )
                    with create_guard:
                        results.append(dict(result))
                    break
                except InstanceLockError:
                    time.sleep(0.005)
            else:
                raise AssertionError("shared singleton lock never became available")
        except BaseException as error:
            with create_guard:
                errors.append(error)
        finally:
            connection.close()

    workers = [threading.Thread(target=fresh_coordinator_attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert not [worker for worker in workers if worker.is_alive()]
    assert not errors
    assert len(results) == 2
    assert {result["outcome"] for result in results} == {"held"}
    assert sorted(result["actions_attempted"] for result in results) == [0, 1]
    assert len({result["operation_key"] for result in results}) == 1
    assert create_calls == [results[0]["operation_key"]]

    reopened = EvidenceStore.open(database)
    try:
        state = reopened.read_scope(seed.scope)
        corrections = [op for op in state["operations"]
                       if op.effect == "create_held" and op.target.get("kind") == "paid_correction_v1"]
        charges = [event for event in state["budget_events"]
                   if event["event_id"].startswith("review_corrections:")]
        correction_members = [member for member in state["members"]
                              if member.work_association.startswith("paid-correction:")]
        paid_members = [member for member in state["members"]
                        if member.task_id == paid_task and member.role == "paid_review"]
        assert len(corrections) == len(charges) == len(correction_members) == len(paid_members) == 1
        assert corrections[0].key == charges[0]["native_source_id"] == results[0]["operation_key"]
        assert charges[0]["generation"] == paid_members[0].generation
        assert correction_members[0].generation == paid_members[0].generation + 1
        assert corrections[0].readback["head_sha"] == head.head_sha
        assert corrections[0].readback["task_id"] in cards
        assert cards[corrections[0].readback["task_id"]].native_task["status"] == "blocked"
    finally:
        reopened.close()
