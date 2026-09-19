from __future__ import annotations

import argparse
import unittest
import json
import math
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.daemon import SQLITE_INT_MAX, SchedulerDaemon, _saturating_exponential_backoff
from local_first_orchestrator.cli import register_cli
from local_first_orchestrator.ledger import Ledger, saturating_non_negative_counter, saturating_non_negative_counter_increment
from local_first_orchestrator.scheduler import ProcessNextResult, ProcessNextScheduler
from local_first_orchestrator.states import CanonicalState


class FakeScheduler:
    def __init__(self, outcome):
        self.outcome = outcome

    def process_next(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Board:
    timeout_seconds = 1

    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    def set_state(self, external_id: str, state, **kwargs) -> None:
        self.states.append((external_id, str(state)))

    def find_comment_marker(self, external_id: str, marker: str):
        return "not_found"

    def deliver_comment(self, external_id: str, body: str, **kwargs) -> None:
        self.comments.append((external_id, body))

    def create_microticket(self, *args, **kwargs):
        return "external-generated"


class SchedulerDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "ledger.db"
        self.ledger = Ledger(self.database)
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def _daemon_status_kwargs(self, worker_id: str = "daemon", **overrides):
        values = {
            "worker_id": worker_id,
            "status": "starting",
            "last_status": None,
            "last_stage": None,
            "last_ticket_id": None,
            "reason_category": None,
            "reason": None,
            "consecutive_undispatchable": 0,
            "iterations": 0,
            "successful_ticks": 0,
            "idle_ticks": 0,
            "busy_ticks": 0,
            "paused_ticks": 0,
            "transient_errors": 0,
            "consecutive_errors": 0,
            "last_error": None,
            "last_tick_started_at": None,
            "last_tick_completed_at": None,
        }
        values.update(overrides)
        return values

    def _valid_daemon_row(self, **overrides):
        row = {
            "worker_id": "daemon",
            "status": "starting",
            "last_status": None,
            "last_stage": None,
            "last_ticket_id": None,
            "reason_category": None,
            "reason": None,
            "consecutive_undispatchable": 0,
            "iterations": 0,
            "successful_ticks": 0,
            "idle_ticks": 0,
            "busy_ticks": 0,
            "paused_ticks": 0,
            "transient_errors": 0,
            "consecutive_errors": 0,
            "last_error": None,
            "last_tick_started_at": None,
            "last_tick_completed_at": None,
            "updated_at": 0,
        }
        row.update(overrides)
        return row

    def test_run_rejects_non_builtin_non_negative_int_before_running_or_loop(self) -> None:
        calls: list[str] = []

        class IntSubclass(int):
            pass

        daemon: SchedulerDaemon

        def stop_after_sleep(_delay: float) -> None:
            daemon.request_stop()

        daemon = SchedulerDaemon(
            self.ledger,
            lambda: calls.append("tick") or FakeScheduler(ProcessNextResult("no_work")),
            worker_id="daemon",
            sleep=stop_after_sleep,
        )
        invalid_values = (True, IntSubclass(1), 1.0, 1.5, math.nan, math.inf, -math.inf, "1", -1)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    daemon.run(max_iterations=value)  # type: ignore[arg-type]
                self.assertFalse(daemon.health().running)
        self.assertEqual(calls, [])

    def test_run_zero_ticks_and_failed_attempts_consume_invocation_local_budget(self) -> None:
        calls: list[str] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: calls.append("tick") or FakeScheduler(RuntimeError("tick failed")),
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )

        daemon.run(max_iterations=0)
        self.assertEqual(calls, [])
        daemon.run(max_iterations=3)
        self.assertEqual(calls, ["tick", "tick", "tick"])

    def test_daemon_timestamp_writes_accept_none_and_finite_reals_and_normalize_to_float(self) -> None:
        for field in ("last_tick_started_at", "last_tick_completed_at"):
            for value in (None, 7, 7.5):
                with self.subTest(field=field, value=value):
                    worker_id = f"timestamp-{field}-{value}"
                    row = self.ledger.upsert_daemon_status(**self._daemon_status_kwargs(worker_id, **{field: value}))
                    self.assertIsNone(row[field]) if value is None else self.assertEqual(row[field], float(value))

    def test_daemon_timestamp_writes_reject_invalid_values_before_sqlite_binding(self) -> None:
        invalid_values = (True, float("nan"), float("inf"), float("-inf"), "7", object())
        for field in ("last_tick_started_at", "last_tick_completed_at"):
            for index, value in enumerate(invalid_values):
                with self.subTest(field=field, value=value):
                    worker_id = f"invalid-{field}-{index}"
                    with self.assertRaises(ValueError):
                        self.ledger.upsert_daemon_status(**self._daemon_status_kwargs(worker_id, **{field: value}))
                    self.assertIsNone(self.ledger.connection.execute("SELECT 1 FROM daemon_status WHERE worker_id=?", (worker_id,)).fetchone())

    def test_operator_daemon_reader_rejects_invalid_persisted_timestamps_without_leaking_values(self) -> None:
        invalid_values = (True, float("nan"), float("inf"), float("-inf"), "7", object())
        for field in ("last_tick_started_at", "last_tick_completed_at"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    validated = Ledger._operator_daemon_row(self._valid_daemon_row(**{field: value}))
                    self.assertEqual(validated["status"], "invalid_persisted_state")
                    self.assertEqual(validated["reason_category"], "malformed_persisted_state")
                    self.assertNotIn(value, validated.values())

    def test_operator_daemon_reader_normalizes_finite_persisted_timestamps(self) -> None:
        for field in ("last_tick_started_at", "last_tick_completed_at"):
            for value in (None, 7, 7.5):
                with self.subTest(field=field, value=value):
                    validated = Ledger._operator_daemon_row(self._valid_daemon_row(**{field: value}))
                    self.assertIsNone(validated[field]) if value is None else self.assertEqual(validated[field], float(value))

    def test_saturating_backoff_preserves_zero_base_semantics(self) -> None:
        self.assertEqual(_saturating_exponential_backoff(0.0, 10**9, 4.0), 0.0)
        self.assertEqual(_saturating_exponential_backoff(1.0, 10, 0.0), 0.0)

    def test_saturating_backoff_doubles_normally_before_capping(self) -> None:
        self.assertEqual(_saturating_exponential_backoff(1.0, 0, 4.0), 1.0)
        self.assertEqual(_saturating_exponential_backoff(1.0, 1, 4.0), 2.0)
        self.assertEqual(_saturating_exponential_backoff(1.0, 2, 4.0), 4.0)
        self.assertEqual(_saturating_exponential_backoff(1.0, 3, 4.0), 4.0)

    def test_saturating_backoff_caps_huge_exponent_without_overflow(self) -> None:
        self.assertEqual(_saturating_exponential_backoff(1.0, 1025, 4.0), 4.0)
        self.assertEqual(_saturating_exponential_backoff(1.0, 10**9, 4.0), 4.0)

    def test_idle_busy_and_completed_ticks_use_expected_sleep_policy(self) -> None:
        outcomes = [ProcessNextResult("no_work"), ProcessNextResult("busy"), ProcessNextResult("completed", "validation", "T")]
        sleeps: list[float] = []

        def factory():
            return FakeScheduler(outcomes.pop(0))

        daemon = SchedulerDaemon(
            self.ledger,
            factory,
            worker_id="daemon",
            idle_sleep_seconds=2.0,
            busy_sleep_seconds=0.5,
            sleep=sleeps.append,
            clock=lambda: 100.0,
        )
        health = daemon.run(max_iterations=3)
        self.assertEqual(sleeps, [2.0, 0.5])
        self.assertEqual(health.iterations, 3)
        self.assertEqual(health.successful_ticks, 3)
        self.assertEqual(health.idle_ticks, 1)
        self.assertEqual(health.busy_ticks, 1)
        self.assertEqual(health.last_status, "completed")
        self.assertEqual(health.last_stage, "validation")
        self.assertFalse(health.running)

    def test_idle_tick_can_advance_one_authorized_external_task(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: calls.append("dispatch") or "H-1",
            worker_id="daemon",
            idle_sleep_seconds=2.0,
            sleep=sleeps.append,
            clock=lambda: 100.0,
        )
        health = daemon.run(max_iterations=1)
        self.assertEqual(calls, ["dispatch"])
        self.assertEqual(sleeps, [])
        self.assertEqual(health.last_status, "external_progress")
        self.assertEqual(health.last_stage, "hermes_dispatch")

    def test_restart_run_budget_is_invocation_local_not_durable_lifetime(self) -> None:
        self.ledger.upsert_daemon_status(
            worker_id="daemon",
            status="no_work",
            last_status="no_work",
            last_stage="validation",
            last_ticket_id=None,
            reason_category=None,
            reason=None,
            consecutive_undispatchable=0,
            iterations=7,
            successful_ticks=7,
            idle_ticks=7,
            busy_ticks=0,
            paused_ticks=0,
            transient_errors=0,
            consecutive_errors=0,
            last_error=None,
            last_tick_started_at=100.0,
            last_tick_completed_at=100.0,
        )
        scheduler_calls: list[str] = []

        def factory():
            scheduler_calls.append("process_next")
            return FakeScheduler(ProcessNextResult("no_work"))

        daemon = SchedulerDaemon(
            self.ledger,
            factory,
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 101.0,
        )

        health = daemon.run(max_iterations=1)

        self.assertEqual(scheduler_calls, ["process_next"])
        self.assertEqual(health.iterations, 8)
        self.assertEqual(self.ledger.operator_status()["daemon_status"][0]["iterations"], 8)

    def test_interrupted_undispatchable_sleep_commits_counter_and_restarts_backoff(self) -> None:
        def interrupt(_delay: float) -> None:
            raise SystemExit("interrupt during backoff")

        first = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: {"status": "authorized_undispatchable", "stage": "hermes_dispatch"},
            worker_id="daemon",
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=2.0,
            sleep=interrupt,
            clock=lambda: 100.0,
        )
        with self.assertRaises(SystemExit):
            first.run(max_iterations=1)

        self.ledger.close()
        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        row = self.ledger.operator_status()["daemon_status"][0]
        self.assertEqual(row["status"], "authorized_undispatchable")
        self.assertEqual(row["consecutive_undispatchable"], 1)

        sleeps: list[float] = []
        second = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: {"status": "authorized_undispatchable", "stage": "hermes_dispatch"},
            worker_id="daemon",
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=2.0,
            sleep=sleeps.append,
            clock=lambda: 101.0,
        )
        second.run(max_iterations=2)
        self.assertEqual(sleeps, [2.0, 2.0])

        self.ledger.close()
        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        third_sleeps: list[float] = []
        third = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: {"status": "authorized_undispatchable", "stage": "hermes_dispatch"},
            worker_id="daemon",
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=2.0,
            sleep=third_sleeps.append,
            clock=lambda: 102.0,
        )
        third.run(max_iterations=3)
        self.assertEqual(third_sleeps, [2.0, 2.0, 2.0])

    def test_sqlite_signed_max_undispatchable_counter_saturates_before_persist_and_sleep(self) -> None:
        sqlite_int_max = SQLITE_INT_MAX
        self.ledger.upsert_daemon_status(
            worker_id="daemon",
            status="authorized_undispatchable",
            last_status="authorized_undispatchable",
            last_stage="hermes_dispatch",
            last_ticket_id=None,
            reason_category="no_dispatchable_plan",
            reason="authorized work has no dispatchable plan",
            consecutive_undispatchable=sqlite_int_max,
            iterations=sqlite_int_max,
            successful_ticks=sqlite_int_max,
            idle_ticks=sqlite_int_max,
            busy_ticks=sqlite_int_max,
            paused_ticks=sqlite_int_max,
            transient_errors=sqlite_int_max,
            consecutive_errors=sqlite_int_max,
            last_error=None,
            last_tick_started_at=100.0,
            last_tick_completed_at=100.0,
        )
        sleeps: list[float] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: {"status": "authorized_undispatchable", "stage": "hermes_dispatch"},
            worker_id="daemon",
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=4.0,
            sleep=sleeps.append,
            clock=lambda: 101.0,
        )

        self.assertEqual(daemon.health().iterations, sqlite_int_max)
        self.assertEqual(daemon.health().consecutive_undispatchable, sqlite_int_max)
        daemon.run_iteration()

        self.assertEqual(sleeps, [4.0])
        row = self.ledger.operator_status()["daemon_status"][0]
        for field in ("consecutive_undispatchable", "iterations", "successful_ticks", "idle_ticks", "busy_ticks", "paused_ticks", "transient_errors"):
            self.assertEqual(row[field], sqlite_int_max)
        self.assertEqual(row["consecutive_errors"], 0)

    def test_sqlite_signed_max_error_counters_rehydrate_and_backoff_without_overflow(self) -> None:
        sqlite_int_max = SQLITE_INT_MAX
        self.ledger.upsert_daemon_status(
            worker_id="daemon",
            status="error",
            last_status=None,
            last_stage=None,
            last_ticket_id=None,
            reason_category=None,
            reason=None,
            consecutive_undispatchable=sqlite_int_max,
            iterations=sqlite_int_max,
            successful_ticks=sqlite_int_max,
            idle_ticks=sqlite_int_max,
            busy_ticks=sqlite_int_max,
            paused_ticks=sqlite_int_max,
            transient_errors=sqlite_int_max,
            consecutive_errors=sqlite_int_max,
            last_error="persisted error",
            last_tick_started_at=100.0,
            last_tick_completed_at=100.0,
        )
        sleeps: list[float] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(RuntimeError("next error")),
            worker_id="daemon",
            error_backoff_seconds=1.0,
            max_error_backoff_seconds=4.0,
            sleep=sleeps.append,
            clock=lambda: 101.0,
        )

        health = daemon.health()
        self.assertEqual(health.iterations, sqlite_int_max)
        self.assertEqual(health.transient_errors, sqlite_int_max)
        self.assertEqual(health.consecutive_errors, sqlite_int_max)
        with self.assertRaises(RuntimeError):
            daemon.run_iteration()

        self.assertEqual(sleeps, [4.0])
        row = self.ledger.operator_status()["daemon_status"][0]
        self.assertEqual(row["transient_errors"], sqlite_int_max)
        self.assertEqual(row["consecutive_errors"], sqlite_int_max)
        self.assertEqual(row["iterations"], sqlite_int_max)

    def test_daemon_upsert_rejects_sqlite_out_of_range_counters(self) -> None:
        values = {field: 0 for field in ("consecutive_undispatchable", "iterations", "successful_ticks", "idle_ticks", "busy_ticks", "paused_ticks", "transient_errors", "consecutive_errors")}
        for field in values:
            values[field] = 2**63
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.ledger.upsert_daemon_status(
                        worker_id="out-of-range-" + field,
                        status="starting",
                        last_status=None,
                        last_stage=None,
                        last_ticket_id=None,
                        reason_category=None,
                        reason=None,
                        last_error=None,
                        last_tick_started_at=None,
                        last_tick_completed_at=None,
                        **values,
                    )
            values[field] = 0

    def test_out_of_range_persisted_counter_is_not_authority(self) -> None:
        row = {
            "worker_id": "malformed",
            "status": "error",
            "consecutive_undispatchable": SQLITE_INT_MAX + 1,
            "iterations": 0,
            "successful_ticks": 0,
            "idle_ticks": 0,
            "busy_ticks": 0,
            "paused_ticks": 0,
            "transient_errors": 0,
            "consecutive_errors": 0,
        }
        validated = Ledger._operator_daemon_row(row)  # type: ignore[arg-type]
        self.assertEqual(validated["status"], "invalid_persisted_state")
        self.assertEqual(validated["reason_category"], "malformed_persisted_state")

    def test_saturating_counter_helpers_clamp_at_sqlite_signed_max(self) -> None:
        self.assertEqual(saturating_non_negative_counter(SQLITE_INT_MAX + 1), SQLITE_INT_MAX)
        self.assertEqual(saturating_non_negative_counter_increment(SQLITE_INT_MAX), SQLITE_INT_MAX)

        invalid_values = (float("nan"), float("inf"), float("-inf"), -1.0, True)
        for value in invalid_values:
            with self.subTest(base=value):
                with self.assertRaises(ValueError):
                    _saturating_exponential_backoff(value, 0, 1.0)
            with self.subTest(maximum=value):
                with self.assertRaises(ValueError):
                    _saturating_exponential_backoff(1.0, 0, value)
        for exponent in (-1, True, 1.0):
            with self.subTest(exponent=exponent):
                with self.assertRaises(ValueError):
                    _saturating_exponential_backoff(1.0, exponent, 4.0)

    def test_constructor_validates_all_sleep_and_backoff_inputs_before_order_checks(self) -> None:
        fields = (
            "idle_sleep_seconds",
            "busy_sleep_seconds",
            "error_backoff_seconds",
            "max_error_backoff_seconds",
            "undispatchable_backoff_seconds",
            "max_undispatchable_backoff_seconds",
        )
        for field in fields:
            for value in (float("nan"), float("inf"), float("-inf"), -1.0, True):
                with self.subTest(field=field, value=value):
                    kwargs = {field: value}
                    with self.assertRaises(ValueError):
                        SchedulerDaemon(self.ledger, lambda: FakeScheduler(ProcessNextResult("no_work")), worker_id="daemon", **kwargs)

    def test_negative_zero_is_normalized_to_zero_for_backoff_and_constructor(self) -> None:
        self.assertEqual(_saturating_exponential_backoff(-0.0, 10**9, 4.0), 0.0)
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            worker_id="daemon",
            idle_sleep_seconds=-0.0,
            busy_sleep_seconds=-0.0,
            error_backoff_seconds=-0.0,
            max_error_backoff_seconds=0.0,
            undispatchable_backoff_seconds=-0.0,
            max_undispatchable_backoff_seconds=0.0,
        )
        self.assertEqual(daemon.idle_sleep_seconds, 0.0)
        self.assertEqual(daemon.error_backoff_seconds, 0.0)

    def test_rehydrated_large_undispatchable_counter_is_capped_without_overflow(self) -> None:
        self.ledger.upsert_daemon_status(
            worker_id="daemon",
            status="authorized_undispatchable",
            last_status="authorized_undispatchable",
            last_stage="hermes_dispatch",
            last_ticket_id=None,
            reason_category="no_dispatchable_plan",
            reason="authorized work has no dispatchable plan",
            consecutive_undispatchable=1025,
            iterations=1,
            successful_ticks=1,
            idle_ticks=0,
            busy_ticks=0,
            paused_ticks=0,
            transient_errors=0,
            consecutive_errors=0,
            last_error=None,
            last_tick_started_at=100.0,
            last_tick_completed_at=100.0,
        )
        sleeps: list[float] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: {"status": "authorized_undispatchable", "stage": "hermes_dispatch"},
            worker_id="daemon",
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=4.0,
            sleep=sleeps.append,
            clock=lambda: 101.0,
        )

        daemon.run_iteration()

        self.assertEqual(sleeps, [4.0])
        row = self.ledger.operator_status()["daemon_status"][0]
        self.assertEqual(row["status"], "authorized_undispatchable")
        self.assertEqual(row["consecutive_undispatchable"], 1026)

    def test_authorized_but_undispatchable_is_durable_and_backed_off(self) -> None:
        sleeps: list[float] = []
        outcomes = [{"status": "authorized_undispatchable", "stage": "hermes_dispatch", "reason_category": "no_dispatchable_plan"}, {"status": "authorized_undispatchable", "stage": "hermes_dispatch", "reason_category": "no_dispatchable_plan"}, {"status": "no_authorized_work"}]
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            external_progress_runner=lambda: outcomes.pop(0),
            worker_id="daemon",
            idle_sleep_seconds=7.0,
            undispatchable_backoff_seconds=1.0,
            max_undispatchable_backoff_seconds=4.0,
            sleep=sleeps.append,
            clock=lambda: 100.0,
        )
        daemon.run(max_iterations=3)
        self.assertEqual(sleeps, [1.0, 2.0, 7.0])
        status = self.ledger.operator_status()["daemon_status"]
        self.assertEqual(status[0]["status"], "no_work")
        self.assertEqual(status[0]["consecutive_undispatchable"], 0)

    def test_daemon_cli_rejects_unsafe_worker_id_during_argument_parsing(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        for value in (
            "password=FAKE_CLI_SECRET", "PaSsWoRd-PRIMARY_SECRET_7d2e", "SeCrEt-PRIMARY_SECRET_7d2e",
            "API_KEY-PRIMARY_SECRET_7d2e", "PRIVATE_KEY-PRIMARY_SECRET_7d2e", "bad worker", "w" * 129,
        ):
            with self.subTest(worker_id=value[:32]), self.assertRaises(SystemExit):
                parser.parse_args(["--database", str(self.database), "daemon", "--worker-id", value])

    def test_worker_id_rejects_unsafe_or_overlong_identity_without_persistence(self) -> None:
        unsafe = (
            "password=FAKE_WORKER_SECRET",
            "PaSsWoRd-PRIMARY_SECRET_7d2e",
            "SeCrEt-PRIMARY_SECRET_7d2e",
            "API_KEY-PRIMARY_SECRET_7d2e",
            "PRIVATE_KEY-PRIMARY_SECRET_7d2e",
            "token:'FAKE_WORKER_SECRET'",
            "bad worker",
            "w" * 129,
        )
        for value in unsafe:
            with self.subTest(worker_id=value[:32]):
                with self.assertRaises(ValueError):
                    SchedulerDaemon(
                        self.ledger,
                        lambda: FakeScheduler(ProcessNextResult("no_work")),
                        worker_id=value,
                        sleep=lambda _delay: None,
                        clock=lambda: 100.0,
                    )
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM daemon_status").fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    self.ledger.upsert_daemon_status(**self._daemon_status_kwargs(value))
                self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM daemon_status").fetchone()[0], 0)

    def test_legacy_credential_shaped_authority_ids_fail_closed_case_insensitively(self) -> None:
        values = (
            "password-PRIMARY_SECRET_7d2e",
            "PaSsWoRd-PRIMARY_SECRET_7d2e",
            "secret-PRIMARY_SECRET_7d2e",
            "SeCrEt-PRIMARY_SECRET_7d2e",
            "api_key-PRIMARY_SECRET_7d2e",
            "API_KEY-PRIMARY_SECRET_7d2e",
            "private_key-PRIMARY_SECRET_7d2e",
            "PRIVATE_KEY-PRIMARY_SECRET_7d2e",
        )
        for index, value in enumerate(values):
            with self.subTest(value=value):
                self.ledger.connection.execute(
                    "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (value, "idle", 0, 0, 0, 0, 0, 0, 0, 0, index + 1),
                )
                for surface in (
                    self.ledger.daemon_status_rows(limit=1),
                    self.ledger.status()["daemon_status"],
                    self.ledger.operator_status()["daemon_status"],
                ):
                    row = surface[0]
                    self.assertEqual(row["worker_id"], "<malformed>")
                    self.assertEqual(row["status"], "invalid_persisted_state")
                    self.assertNotIn("PRIMARY_SECRET_7d2e", json.dumps(row, sort_keys=True))
                self.ledger.connection.execute("DELETE FROM daemon_status")

    def test_required_status_labels_reject_credential_shaped_values_case_insensitively(self) -> None:
        values = (
            "PaSsWoRd-STATUS_SECRET_7d2e",
            "SeCrEt-STATUS_SECRET_7d2e",
            "API_KEY-STATUS_SECRET_7d2e",
            "PRIVATE_KEY-STATUS_SECRET_7d2e",
        )
        for index, value in enumerate(values):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.ledger.upsert_daemon_status(**self._daemon_status_kwargs(status=value))
                self.ledger.connection.execute(
                    "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"safe-worker-{index}", value, 0, 0, 0, 0, 0, 0, 0, 0, index + 1),
                )
                row = self.ledger.operator_status()["daemon_status"][0]
                self.assertEqual(row["worker_id"], "<malformed>")
                self.assertEqual(row["status"], "invalid_persisted_state")
                self.assertNotIn("STATUS_SECRET_7d2e", json.dumps(row, sort_keys=True))
                self.ledger.connection.execute("DELETE FROM daemon_status")

    def test_legacy_updated_at_malformed_values_fail_closed_without_exposure(self) -> None:
        malformed_values = (
            -1,
            1.5,
            float("inf"),
            "9223372036854775808",
            "token=FINAL_CLI_SECRET_7d2e",
        )
        for index, value in enumerate(malformed_values):
            with self.subTest(value=repr(value)):
                self.ledger.connection.execute(
                    "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"legacy-updated-{index}", "idle", 0, 0, 0, 0, 0, 0, 0, 0, value),
                )
                for surface in (
                    self.ledger.daemon_status_rows(limit=1),
                    self.ledger.status()["daemon_status"],
                    self.ledger.operator_status()["daemon_status"],
                ):
                    row = surface[0]
                    self.assertEqual(row["worker_id"], "<malformed>")
                    self.assertEqual(row["status"], "invalid_persisted_state")
                    rendered = json.dumps(row, sort_keys=True)
                    self.assertNotIn("FINAL_CLI_SECRET_7d2e", rendered)
                    self.assertNotIn("token=", rendered)
                self.ledger.connection.execute("DELETE FROM daemon_status")

    def test_operator_daemon_reader_rejects_unrepresentable_updated_at_types(self) -> None:
        base = {
            "worker_id": "synthetic-worker",
            "status": "idle",
            "last_status": None,
            "last_stage": None,
            "last_ticket_id": None,
            "reason_category": None,
            "reason": None,
            "consecutive_undispatchable": 0,
            "iterations": 0,
            "successful_ticks": 0,
            "idle_ticks": 0,
            "busy_ticks": 0,
            "paused_ticks": 0,
            "transient_errors": 0,
            "consecutive_errors": 0,
            "last_error": None,
            "last_tick_started_at": None,
            "last_tick_completed_at": None,
            "updated_at": 1,
        }
        for value in (None, True, False, -1, 2**63, 1.5, float("nan"), float("inf"), "token=SYNTHETIC_SECRET"):
            with self.subTest(value=repr(value)):
                row = dict(base)
                row["updated_at"] = value
                rendered = Ledger._operator_daemon_row(row)
                self.assertEqual(rendered["worker_id"], "<malformed>")
                self.assertEqual(rendered["status"], "invalid_persisted_state")
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(rendered, sort_keys=True))

    def test_in_memory_health_sanitizes_observational_result_fields(self) -> None:
        stage_secret = 'password="FAKE_STAGE_SECRET"'
        ticket_secret = "Authorization: Bearer FAKE_TICKET_BEARER"
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("busy", stage_secret, ticket_secret)),
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )
        result = daemon.run_iteration()
        self.assertEqual(result.stage, stage_secret)
        health = daemon.health()
        status = daemon.status()
        cli_json = json.dumps({"health": asdict(health)}, sort_keys=True)
        for rendered in (health.last_stage, health.last_ticket_id, status["health"]["last_stage"], status["health"]["last_ticket_id"], cli_json):
            self.assertNotIn("FAKE_STAGE_SECRET", rendered or "")
            self.assertNotIn("FAKE_TICKET_BEARER", rendered or "")
        self.assertEqual(health.last_stage, "[REDACTED]")
        self.assertEqual(health.last_ticket_id, "[REDACTED]")

        daemon._last_result = ProcessNextResult("token=FAKE_STATUS_SECRET", "safe-stage", "safe-ticket")
        health = daemon.health()
        self.assertEqual(health.last_status, "[REDACTED]")
        self.assertNotIn("FAKE_STATUS_SECRET", json.dumps(asdict(health), sort_keys=True))

    def test_legacy_daemon_rows_fail_closed_on_unsafe_required_identity_and_sanitize_optional_text(self) -> None:
        credential_values = (
            "password=FAKE_PASSWORD_UNQUOTED",
            'password="FAKE_PASSWORD_QUOTED"',
            "token=FAKE_TOKEN_UNQUOTED",
            "token='FAKE_TOKEN_QUOTED'",
            "secret=FAKE_SECRET_UNQUOTED",
            'secret="FAKE_SECRET_QUOTED"',
            "api_key=FAKE_API_UNQUOTED",
            "api_key='FAKE_API_QUOTED'",
            "private_key=FAKE_PRIVATE_UNQUOTED",
            'private_key="FAKE_PRIVATE_QUOTED"',
            "Authorization: Bearer FAKE_BEARER",
            "Authorization: Basic FAKE_BASIC",
            "https://user:FAKE_URL_PASSWORD@example.invalid/path",
        )
        optional_fields = ("last_status", "last_stage", "last_ticket_id", "reason_category", "reason", "last_error")

        for index, value in enumerate(credential_values):
            worker = f"legacy-worker-{index}"
            self.ledger.connection.execute(
                "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (value, "idle", 0, 0, 0, 0, 0, 0, 0, 0, index + 1),
            )
            row = self.ledger.daemon_status_rows(limit=1)[0]
            self.assertEqual(row["worker_id"], "<malformed>")
            self.assertEqual(row["status"], "invalid_persisted_state")
            self.ledger.connection.execute("DELETE FROM daemon_status")

            self.ledger.connection.execute(
                "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (worker, value, 0, 0, 0, 0, 0, 0, 0, 0, index + 1),
            )
            for surface in (self.ledger.daemon_status_rows(limit=1), self.ledger.status()["daemon_status"], self.ledger.operator_status()["daemon_status"]):
                row = surface[0]
                self.assertEqual(row["worker_id"], "<malformed>")
                self.assertEqual(row["status"], "invalid_persisted_state")
            self.ledger.connection.execute("DELETE FROM daemon_status")

            for field in optional_fields:
                columns = ["worker_id", "status", field, "consecutive_undispatchable", "iterations", "successful_ticks", "idle_ticks", "busy_ticks", "paused_ticks", "transient_errors", "consecutive_errors", "updated_at"]
                placeholders = ",".join("?" for _ in columns)
                values = [worker, "idle", value, 0, 0, 0, 0, 0, 0, 0, 0, index + 1]
                self.ledger.connection.execute(
                    f"INSERT INTO daemon_status({','.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                for surface in (self.ledger.daemon_status_rows(limit=1), self.ledger.status()["daemon_status"], self.ledger.operator_status()["daemon_status"]):
                    rendered = json.dumps(surface[0], sort_keys=True)
                    self.assertNotIn("FAKE_", rendered)
                    self.assertNotIn("user:FAKE", rendered)
                self.ledger.connection.execute("DELETE FROM daemon_status")

    def test_daemon_error_sanitization_is_consistent_across_health_status_persistence_and_cli(self) -> None:
        supplied_values = (
            "FAKE_PASSWORD_UNQUOTED",
            "FAKE_PASSWORD_QUOTED",
            "FAKE_TOKEN_UNQUOTED",
            "FAKE_TOKEN_QUOTED",
            "FAKE_API_KEY_UNQUOTED",
            "FAKE_API_KEY_QUOTED",
            "FAKE_PRIVATE_KEY_UNQUOTED",
            "FAKE_PRIVATE_KEY_QUOTED",
        )
        message = (
            "\x00  scheduler\tfailed\n"
            "password=FAKE_PASSWORD_UNQUOTED password: \"FAKE_PASSWORD_QUOTED\" "
            "token:FAKE_TOKEN_UNQUOTED token = 'FAKE_TOKEN_QUOTED' "
            "api_key=FAKE_API_KEY_UNQUOTED api_key : \"FAKE_API_KEY_QUOTED\" "
            "private_key:FAKE_PRIVATE_KEY_UNQUOTED private_key = 'FAKE_PRIVATE_KEY_QUOTED' "
            + ("safe-context " * 100)
        )
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(RuntimeError(message)),
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )

        with self.assertRaises(RuntimeError):
            daemon.run_iteration()

        health = daemon.health()
        status = daemon.status()
        persisted = self.ledger.operator_status()["daemon_status"][0]
        cli_serialized = json.dumps({"health": asdict(health)}, sort_keys=True)
        cli_health_error = json.loads(cli_serialized)["health"]["last_error"]
        observed = (health.last_error, status["health"]["last_error"], persisted["last_error"], cli_health_error)
        for value in observed:
            self.assertIsNotNone(value)
            self.assertLessEqual(len(value), 500)
            self.assertTrue(value.startswith("RuntimeError: scheduler failed"))
            for supplied in supplied_values:
                self.assertNotIn(supplied, value)
        for supplied in supplied_values:
            self.assertNotIn(supplied, cli_serialized)
        self.assertNotIn("\x00", health.last_error or "")
        self.assertNotIn("\t", health.last_error or "")
        self.assertNotIn("\n", health.last_error or "")
        self.assertEqual(health.last_error, status["health"]["last_error"])
        self.assertEqual(health.last_error, persisted["last_error"])

    def test_persisted_daemon_error_survives_new_ledger_and_is_bounded(self) -> None:
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(RuntimeError("secret-token-should-not-be-unbounded")),
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )
        daemon.run(max_iterations=1)
        self.ledger.close()
        reopened = Ledger(self.database)
        try:
            reopened.migrate()
            row = reopened.operator_status()["daemon_status"][0]
            self.assertEqual(row["status"], "error")
            self.assertTrue(row["last_error"].startswith("RuntimeError: "))
            self.assertLessEqual(len(row["last_error"]), 500)
            self.assertNotIn("secret-token-should-not-be-unbounded", row["last_error"])
        finally:
            reopened.close()

    def test_daemon_status_migration_is_idempotent_and_malformed_rows_are_safe(self) -> None:
        self.ledger.migrate()
        self.ledger.migrate()
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2").fetchone()[0], 1)
        self.ledger.connection.execute(
            "INSERT INTO daemon_status(worker_id,status,consecutive_undispatchable,iterations,successful_ticks,idle_ticks,busy_ticks,paused_ticks,transient_errors,consecutive_errors,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("malformed", "bad", "not-an-int", 0, 0, 0, 0, 0, 0, 0, 1),
        )
        row = self.ledger.operator_status()["daemon_status"][0]
        self.assertEqual(row["status"], "invalid_persisted_state")
        self.assertEqual(row["reason_category"], "malformed_persisted_state")

    def test_dispatch_authority_requires_durable_release_or_repair_activation(self) -> None:
        imported = self.ledger.create_ticket(
            title="imported",
            state=CanonicalState.READY_LOCAL,
            external_id="H-imported",
        )
        self.assertEqual(self.ledger.dispatchable_external_task_ids(), ())

        repair = self.ledger.create_ticket(
            title="repair",
            state=CanonicalState.REPAIRING,
            external_id="H-original",
        )
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,pre_diff_hash,created_at) VALUES (?,?,?,?,?,?,?)",
            (repair, 1, "a" * 40, "repair/branch", str(self.root / "worktree"), "b" * 64, 1),
        )
        self.ledger.record_runtime_stage(
            repair,
            "generated-repair-activation-1",
            '{"external_task_id":"H-repair","attempt_number":1}',
            attempt_number=1,
            base_sha="a" * 40,
        )
        self.assertEqual(self.ledger.dispatchable_external_task_ids(), ("H-repair",))
        self.assertNotIn("H-imported", self.ledger.dispatchable_external_task_ids())

    def test_transient_errors_backoff_exponentially_and_success_resets_counter(self) -> None:
        outcomes = [RuntimeError("one"), RuntimeError("two"), ProcessNextResult("no_work"), RuntimeError("three")]
        sleeps: list[float] = []

        def factory():
            return FakeScheduler(outcomes.pop(0))

        daemon = SchedulerDaemon(
            self.ledger,
            factory,
            worker_id="daemon",
            idle_sleep_seconds=7.0,
            error_backoff_seconds=1.0,
            max_error_backoff_seconds=4.0,
            sleep=sleeps.append,
            clock=lambda: 100.0,
        )
        health = daemon.run(max_iterations=4, continue_on_error=True)
        self.assertEqual(sleeps, [1.0, 2.0, 7.0, 1.0])
        self.assertEqual(health.transient_errors, 3)
        self.assertEqual(health.consecutive_errors, 1)
        self.assertIn("RuntimeError: three", health.last_error or "")

    def test_graceful_stop_is_observed_between_bounded_ticks(self) -> None:
        sleeps: list[float] = []
        daemon: SchedulerDaemon

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            daemon.request_stop()

        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            worker_id="daemon",
            idle_sleep_seconds=1.0,
            sleep=sleep,
            clock=lambda: 100.0,
        )
        health = daemon.run()
        self.assertEqual(health.iterations, 1)
        self.assertTrue(health.stop_requested)
        self.assertEqual(sleeps, [1.0])
        self.assertFalse(health.running)

    def test_pause_uses_existing_one_tick_semantics_and_does_not_admit_new_work(self) -> None:
        ticket = self.ledger.create_ticket(
            title="paused-ready",
            state=CanonicalState.READY_LOCAL,
            external_id="external-paused",
            contract={
                "objective": "paused",
                "criterion_ids": ["AC-1"],
                "primary_symbol": "app.py::value",
                "allowed_files": ["app.py"],
                "forbidden_changes": [],
                "patch_budget": {"max_files": 1, "max_changed_lines": 10},
                "verification": {"commands": [["python", "-c", "pass"]]},
                "risk": "low",
                "review_required": True,
                "max_attempts": 1,
                "dependencies": [],
            },
        )
        self.ledger.bind_runtime(ticket, str(self.root), "a" * 40)
        self.ledger.pause("test", reason="daemon pause test")
        sleeps: list[float] = []
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: ProcessNextScheduler(self.ledger, Board(), worker_id="paused", lease_seconds=30, clock=lambda: 100),
            worker_id="daemon",
            idle_sleep_seconds=1.0,
            sleep=sleeps.append,
            clock=lambda: 100.0,
        )
        health = daemon.run(max_iterations=1)
        self.assertEqual(health.last_status, "paused")
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(health.paused_ticks, 1)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims").fetchone()[0], 0)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], CanonicalState.READY_LOCAL.value)

    def test_restart_is_equivalent_to_later_one_tick_calls(self) -> None:
        board = Board()
        ticket = self.ledger.create_ticket(title="projection", state=CanonicalState.DRAFT, external_id="external-projection")
        self.ledger.transition(ticket, CanonicalState.READY_LOCAL)
        self.assertIsNotNone(self.ledger.plan_projection(ticket, evidence="restart projection"))

        first = SchedulerDaemon(
            self.ledger,
            lambda: ProcessNextScheduler(self.ledger, board, worker_id="daemon-a", lease_seconds=30, clock=lambda: 100),
            worker_id="daemon-a",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )
        first_health = first.run(max_iterations=1)
        self.assertEqual(first_health.last_stage, "state_projection")
        self.assertEqual(len(board.states), 1)
        self.assertEqual(len(board.comments), 0)

        restarted_ledger = Ledger(self.database)
        try:
            second = SchedulerDaemon(
                restarted_ledger,
                lambda: ProcessNextScheduler(restarted_ledger, board, worker_id="daemon-b", lease_seconds=30, clock=lambda: 101),
                worker_id="daemon-b",
                sleep=lambda _delay: None,
                clock=lambda: 101.0,
            )
            second_health = second.run(max_iterations=1)
            self.assertEqual(second_health.last_stage, "evidence_comment")
            self.assertEqual(len(board.states), 1)
            self.assertEqual(len(board.comments), 1)
        finally:
            restarted_ledger.close()

    def test_status_combines_health_and_scheduler_observability(self) -> None:
        daemon = SchedulerDaemon(
            self.ledger,
            lambda: FakeScheduler(ProcessNextResult("no_work")),
            worker_id="daemon",
            sleep=lambda _delay: None,
            clock=lambda: 100.0,
        )
        daemon.run(max_iterations=1)
        status = daemon.status()
        self.assertEqual(status["health"]["worker_id"], "daemon")
        self.assertEqual(status["health"]["last_status"], "no_work")
        self.assertIn("next_stage", status["scheduler"])
        self.assertIn("pending_effects", status["scheduler"])


if __name__ == "__main__":
    unittest.main()
