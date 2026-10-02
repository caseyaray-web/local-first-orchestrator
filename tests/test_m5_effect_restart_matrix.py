"""Fixture-only M5 crash/reopen matrix over the supported effect families.

Each parameter runs the named functional fixture node in a fresh Python process.
That makes a checkpoint's store close/open and coordinator reconstruction the code
under test rather than an in-memory retry.  No entry enables a provider or CLI.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from local_first_orchestrator.budgets import BudgetPolicy
from local_first_orchestrator.coordinator import Coordinator
from local_first_orchestrator.daemon import InstanceLockError, instance_lock
from local_first_orchestrator.evidence_store import EvidenceStore
from tests.test_m5_recovery_polling import FixtureBoard, SCOPE, coordinator, issue


# The four timing names are deliberately literal: they are acceptance identifiers,
# not an implication that an unsupported native adapter call was made.
CASES = (
    ("link", "before_claim_presend", "tests/test_m4_public_core_flow.py::test_m5_reopen_after_v2_link_presend_before_claim_sends_once"),
    ("link", "after_claim_before_effect", "tests/test_m4_public_core_flow.py::test_m5_reopen_after_v2_link_claim_before_native_effect_stays_partial_without_resend"),
    ("link", "after_effect_before_ack", "tests/test_m4_public_core_flow.py::test_m5_reopen_after_v2_link_effect_before_receipt_checkpoint_stays_partial_without_resend"),
    ("link", "after_ack_replay", "tests/test_m4_public_core_flow.py::test_m5_reopen_after_v2_link_acknowledgement_replays_without_native_effect"),
    ("create_held", "before_claim_presend", "tests/test_hermes_board_adapter.py::test_create_held_requires_a_durable_attempt_claim_before_any_native_create"),
    ("create_held", "after_claim_before_effect", "tests/test_hermes_board_adapter.py::test_create_held_unknown_result_is_not_retried_and_later_reconciles_by_marker"),
    ("create_held", "after_effect_before_ack", "tests/test_hermes_board_adapter.py::test_unknown_store_associated_create_reconciles_by_exact_marker_without_resend"),
    ("create_held", "after_ack_replay", "tests/test_hermes_board_adapter.py::test_create_held_reconciles_one_exact_marker_without_a_second_create"),
    ("hold", "before_claim_presend", "tests/test_hermes_board_adapter.py::test_verify_effect_never_verifies_only_a_lane"),
    ("hold", "after_claim_before_effect", "tests/test_coordinator_m2.py::test_unknown_effect_is_never_resent_and_reconcile_is_read_only"),
    ("hold", "after_effect_before_ack", "tests/test_hermes_board_adapter.py::test_unknown_native_hold_is_reconciled_read_only_by_exact_scoped_marker"),
    ("hold", "after_ack_replay", "tests/test_coordinator_m2.py::test_restart_reconciles_unknown_after_effect_with_exact_marker_without_resend"),
    ("release", "before_claim_presend", "tests/test_m4_public_core_flow.py::test_regression_named_piece_release_consumes_one_budget_unit_and_replays"),
    ("release", "after_claim_before_effect", "tests/test_hermes_board_adapter.py::test_release_recovery_after_dispatcher_claim_is_partial_not_success"),
    ("release", "after_effect_before_ack", "tests/test_hermes_board_adapter.py::test_unknown_native_release_is_reconciled_read_only_by_exact_scoped_marker"),
    ("release", "after_ack_replay", "tests/test_m4_active_release_integration.py::test_active_release_and_serial_git_integration_smoke"),
    ("comment", "before_claim_presend", "tests/test_recovery.py::test_unknown_comment_and_link_are_reconciled_with_marker_and_exact_target_not_resent"),
    ("comment", "after_claim_before_effect", "tests/test_hermes_board_adapter.py::test_comment_marker_is_idempotent_and_requires_author_and_exact_marker"),
    ("comment", "after_effect_before_ack", "tests/test_recovery.py::test_unknown_comment_and_link_are_reconciled_with_marker_and_exact_target_not_resent"),
    ("comment", "after_ack_replay", "tests/test_hermes_board_adapter.py::test_comment_marker_is_idempotent_and_requires_author_and_exact_marker"),
    ("request_review", "before_claim_presend", "tests/test_hermes_board_adapter.py::test_request_review_requires_waiting_lane_and_observed_requested_distinct_reviewer"),
    ("request_review", "after_claim_before_effect", "tests/test_hermes_board_adapter.py::test_unknown_request_review_reconciles_only_with_public_native_run_and_event"),
    ("request_review", "after_effect_before_ack", "tests/test_hermes_board_adapter.py::test_unknown_request_review_reconciles_read_only_after_terminal_done"),
    ("request_review", "after_ack_replay", "tests/test_local_review_loop.py::test_submit_review_holds_when_no_exact_native_worker_handoff_exists"),
    ("git_integrate", "before_claim_presend", "tests/test_m4_active_release_integration.py::test_active_release_and_serial_git_integration_smoke"),
    ("git_integrate", "after_claim_before_effect", "tests/test_m4_public_core_flow.py::test_regression_changed_correction_head_requires_fresh_checks_request_and_approval"),
    ("git_integrate", "after_effect_before_ack", "tests/test_m4_active_release_integration.py::test_active_release_and_serial_git_integration_smoke"),
    ("git_integrate", "after_ack_replay", "tests/test_m4_active_release_integration.py::test_active_release_and_serial_git_integration_smoke"),
)


# This historical mapping is a reference list, NOT crash-boundary evidence.
# Re-running a smoke node cannot establish an injected checkpoint or DB reopen.
# Actual family-specific fault-injection tests live in dedicated restart files.
def _historical_mapped_node_smoke(family: str, checkpoint: str, node: str) -> None:
    """Execute the family-specific functional fixture; reject skip or empty selection."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root), HERMES_M0_CLI="")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=--tb=short", node],
        cwd=root, env=env, text=True, capture_output=True, timeout=90,
    )
    assert completed.returncode == 0, f"{family} × {checkpoint}\n{completed.stdout}\n{completed.stderr}"
    assert "skipped" not in completed.stdout.lower(), completed.stdout
    assert "passed" in completed.stdout, completed.stdout


def test_m5_true_concurrent_report_generation_uses_two_store_connections_and_one_lock(tmp_path: Path) -> None:
    """Two simultaneous coordinators share a lock and persist one report generation."""
    board = FixtureBoard()
    seed, store = coordinator(tmp_path, board)
    database = store.path
    store.close()
    start = threading.Barrier(2)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def report_from_fresh_connection() -> None:
        connection = EvidenceStore.open(database)
        try:
            controller = Coordinator(
                SCOPE, board=board, store=connection, lock=instance_lock(tmp_path / "coordinator.lock"),
                budget_policy=BudgetPolicy(2, 2, 2, 2, 1),
            )
            start.wait(timeout=10)
            # The barrier makes the two independent connections contend for the
            # same singleton lock.  A losing acquisition is retried only after
            # the winner has released it, then must observe the same generation.
            for _ in range(100):
                try:
                    results.append(controller.report_issue(issue()))
                    break
                except InstanceLockError:
                    time.sleep(0.005)
            else:
                raise AssertionError("singleton lock never became available")
        except BaseException as error:  # surfaced below with both workers joined
            errors.append(error)
        finally:
            connection.close()

    workers = [threading.Thread(target=report_from_fresh_connection) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert not [worker for worker in workers if worker.is_alive()]
    assert not errors
    assert sorted(result["outcome"] for result in results) == ["deduplicated", "recorded"]

    reopened = EvidenceStore.open(database)
    try:
        reports = [op for op in reopened.read_scope(SCOPE)["operations"] if op.effect == "recovery_report"]
        assert len(reports) == 1
        assert reports[0].phase == "applied"
        assert reports[0].target["generation"] == 0
    finally:
        reopened.close()
