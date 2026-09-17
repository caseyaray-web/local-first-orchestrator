from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.daemon import SchedulerDaemon
from local_first_orchestrator.ledger import Ledger
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
