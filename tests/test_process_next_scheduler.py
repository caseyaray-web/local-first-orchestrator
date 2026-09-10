from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.cli import main as cli_main, register_cli
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState


class Board:
    is_fake = False
    allow_writes = True
    writes_enabled = True
    timeout_seconds = 1

    def __init__(self) -> None:
        self.states: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str, str]] = []

    def set_state(self, ticket_id, state, *, idempotency_key):
        effect = (ticket_id, state.value, idempotency_key)
        if effect not in self.states:
            self.states.append(effect)

    def find_comment_marker(self, ticket_id, marker):
        return "found" if any(ticket_id == row[0] and marker in row[1] for row in self.comments) else "not_found"

    def deliver_comment(self, ticket_id, comment, *, idempotency_key):
        effect = (ticket_id, comment, idempotency_key)
        if effect not in self.comments:
            self.comments.append(effect)

    def create_microticket(self, title, body, *, idempotency_key):
        return f"external-{idempotency_key}"


class ProcessNextSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "ledger.db"
        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        self.board = Board()

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    @staticmethod
    def contract(*, dependencies: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "objective": "bounded scheduler fixture",
            "criterion_ids": ["AC-1"],
            "primary_symbol": "app.py::value",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["no unrelated changes"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-m", "unittest"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 1,
            "dependencies": list(dependencies),
        }

    def ticket(self, title: str, *, state=CanonicalState.DRAFT, dependencies: tuple[str, ...] = ()) -> str:
        ticket = self.ledger.create_ticket(title=title, state=state, external_id=f"external-{title}", contract=self.contract(dependencies=dependencies))
        self.ledger.bind_runtime(ticket, str(self.root), "a" * 40)
        return ticket

    def test_claim_skips_unresolved_dependency_and_is_atomic(self) -> None:
        dependency = self.ticket("dependency", state=CanonicalState.BLOCKED)
        blocked = self.ticket("blocked", dependencies=(dependency,))
        ready = self.ticket("ready")

        claim = self.ledger.claim_next_scheduler_readiness("worker-a", lease_seconds=30, now=100)

        self.assertEqual(claim["ticket_id"], ready)
        self.assertEqual(claim["stage"], "dependency_readiness")
        self.assertEqual(claim["status"], "claimed")
        self.assertIsNone(self.ledger.claim_next_scheduler_readiness("worker-b", lease_seconds=30, now=100))
        self.assertEqual(self.ledger.get_ticket(blocked)["state"], "draft")
        event = self.ledger.events_for(ready)[-1]
        self.assertEqual(event["event_type"], "scheduler_stage_claimed")

    def test_claim_skips_malformed_legacy_dependency_json(self) -> None:
        malformed = self.ticket("malformed")
        self.ledger.connection.execute(
            "UPDATE tickets SET dependencies_json='not-json' WHERE id=?", (malformed,)
        )
        ready = self.ticket("ready")

        claim = self.ledger.claim_next_scheduler_readiness("worker", lease_seconds=30, now=100)

        self.assertEqual(claim["ticket_id"], ready)
        self.assertEqual(self.ledger.get_ticket(malformed)["state"], "draft")

    def test_expired_claim_replays_same_stage_after_restart_without_duplicate_transition(self) -> None:
        ticket = self.ticket("ready")
        first = self.ledger.claim_next_scheduler_readiness("worker-a", lease_seconds=1, now=100)
        self.assertEqual(first["ticket_id"], ticket)
        self.assertEqual(self.ledger.admit_ticket_if_ready(ticket).status, "ready")
        self.ledger.close()

        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        replay = self.ledger.claim_next_scheduler_readiness("worker-b", lease_seconds=10, now=102)
        self.assertEqual(replay["claim_id"], first["claim_id"])
        self.assertEqual(self.ledger.admit_ticket_if_ready(ticket).status, "ready")
        self.ledger.complete_scheduler_claim(first["claim_id"], "worker-b", {"status": "ready"}, now=102)

        transitions = [event for event in self.ledger.events_for(ticket) if event["event_type"] == "state_transition"]
        self.assertEqual(len(transitions), 1)
        row = self.ledger.scheduler_claim(first["claim_id"])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempt_count"], 2)

    def test_process_next_runs_exactly_one_durable_stage_per_tick(self) -> None:
        ticket = self.ticket("ready")
        scheduler = ProcessNextScheduler(self.ledger, self.board, worker_id="scheduler", lease_seconds=30, clock=lambda: 100)

        first = scheduler.process_next()
        self.assertEqual((first.stage, first.status, first.ticket_id), ("dependency_readiness", "completed", ticket))
        self.assertEqual(self.board.states, [])

        second = scheduler.process_next()
        self.assertEqual((second.stage, second.status), ("state_projection", "delivered"))
        self.assertEqual(len(self.board.states), 1)
        self.assertEqual(self.board.comments, [])

        third = scheduler.process_next()
        self.assertEqual((third.stage, third.status), ("evidence_comment", "delivered"))
        self.assertEqual(len(self.board.comments), 1)

    def test_preview_reports_next_stage_without_claims_events_or_effects(self) -> None:
        ticket = self.ticket("ready")
        before_events = self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        readiness = preview_next(self.ledger, now=100)

        self.assertEqual((readiness.status, readiness.next_stage, readiness.ticket_id), ("dry_run", "dependency_readiness", ticket))
        self.assertFalse(readiness.would_execute)
        self.assertFalse(readiness.would_write_board)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_tick_lease").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events)
        self.assertEqual((self.board.states, self.board.comments), ([], []))

        ProcessNextScheduler(self.ledger, self.board, worker_id="scheduler", lease_seconds=30, clock=lambda: 100).process_next()
        projection_before = dict(self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone())
        state = preview_next(self.ledger, now=100)
        projection_after = dict(self.ledger.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone())
        self.assertEqual((state.next_stage, state.ticket_id, state.would_write_board), ("state_projection", ticket, True))
        self.assertEqual(projection_after, projection_before)
        self.assertEqual((self.board.states, self.board.comments), ([], []))

    def test_preview_reports_pause_busy_and_no_work_without_mutation(self) -> None:
        empty = preview_next(self.ledger, now=100)
        self.assertEqual((empty.status, empty.next_stage), ("dry_run", "no_work"))

        self.ledger.claim_scheduler_tick("worker", "token", lease_seconds=30, now=100)
        event_count = self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        busy = preview_next(self.ledger, now=100)
        self.assertEqual((busy.status, busy.next_stage), ("dry_run", "busy"))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
        self.ledger.release_scheduler_tick("worker", "token")

        self.ledger.pause("operator", reason="maintenance")
        event_count = self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        paused = preview_next(self.ledger, now=100)
        self.assertEqual((paused.status, paused.next_stage), ("dry_run", "paused"))
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)

    def test_pause_prevents_all_autonomous_progression(self) -> None:
        ticket = self.ticket("ready")
        self.ledger.pause("operator", reason="maintenance")
        scheduler = ProcessNextScheduler(self.ledger, self.board, worker_id="scheduler", clock=lambda: 100)

        result = scheduler.process_next()

        self.assertEqual((result.stage, result.status), (None, "paused"))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "draft")
        self.assertEqual(self.board.states, [])

    def test_scheduler_lease_covers_bounded_external_effect_horizon(self) -> None:
        with self.assertRaisesRegex(ValueError, "lease does not cover"):
            ProcessNextScheduler(self.ledger, self.board, worker_id="scheduler", lease_seconds=11)

    def test_legacy_tick_lease_schema_migrates_and_discards_unfenced_claim(self) -> None:
        legacy_path = self.root / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute("CREATE TABLE scheduler_tick_lease(id INTEGER PRIMARY KEY, lease_owner TEXT NOT NULL, lease_expires_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
        connection.execute("INSERT INTO scheduler_tick_lease VALUES (1,'legacy-worker',999,100)")
        connection.commit()
        connection.close()

        legacy = Ledger(legacy_path)
        try:
            legacy.migrate()
            columns = {row["name"] for row in legacy.connection.execute("PRAGMA table_info(scheduler_tick_lease)")}
            self.assertIn("lease_token", columns)
            self.assertEqual(legacy.claim_scheduler_tick("worker", "token", lease_seconds=30, now=100), "claimed")
        finally:
            legacy.close()

    def test_status_surfaces_active_tick_and_stage_claim(self) -> None:
        ticket = self.ticket("ready")
        self.assertEqual(self.ledger.claim_scheduler_tick("worker", "token", lease_seconds=30, now=100), "claimed")
        claim = self.ledger.claim_next_scheduler_readiness("worker", lease_seconds=30, now=100)

        status = self.ledger.status()

        self.assertEqual(status["scheduler_tick"]["lease_owner"], "worker")
        self.assertEqual(status["scheduler_claims"][0]["claim_id"], claim["claim_id"])
        self.assertEqual(status["scheduler_claims"][0]["ticket_id"], ticket)

    def test_tick_lease_prevents_overlap_and_recovers_after_expiry(self) -> None:
        ticket = self.ticket("ready")
        self.assertEqual(self.ledger.claim_scheduler_tick("worker-a", "token-a", lease_seconds=1, now=100), "claimed")
        self.assertEqual(self.ledger.claim_scheduler_tick("worker-a", "token-b", lease_seconds=1, now=100), "busy")

        busy = ProcessNextScheduler(self.ledger, self.board, worker_id="worker-b", clock=lambda: 100).process_next()
        self.assertEqual((busy.stage, busy.status), (None, "busy"))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "draft")

        self.assertEqual(self.ledger.claim_scheduler_tick("worker-a", "token-b", lease_seconds=1, now=102), "claimed")
        self.assertFalse(self.ledger.release_scheduler_tick("worker-a", "token-a"))
        active = self.ledger.connection.execute("SELECT lease_token FROM scheduler_tick_lease WHERE id=1").fetchone()
        self.assertEqual(active["lease_token"], "token-b")
        self.assertTrue(self.ledger.release_scheduler_tick("worker-a", "token-b"))

        recovered = ProcessNextScheduler(self.ledger, self.board, worker_id="worker-b", clock=lambda: 102).process_next()
        self.assertEqual((recovered.stage, recovered.status), ("dependency_readiness", "completed"))
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "ready_local")

    def test_cli_exposes_bounded_process_next_with_dry_run_default(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["--database", str(self.database), "process-next"])
        self.assertEqual(args.command, "process-next")
        self.assertFalse(args.execute)
        self.assertFalse(args.allow_board_writes)

        ticket = self.ticket("cli-preview")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--database", str(self.database), "process-next"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual((payload["status"], payload["next_stage"], payload["ticket_id"]), ("dry_run", "dependency_readiness", ticket))
        self.assertFalse(payload["would_execute"])
        self.assertFalse(payload["would_write_board"])

    def test_cli_preview_does_not_create_or_migrate_a_ledger(self) -> None:
        absent = self.root / "absent.db"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--database", str(absent), "process-next"]), 0)
        self.assertFalse(absent.exists())
        self.assertEqual(json.loads(output.getvalue())["next_stage"], "no_work")

        unmigrated = self.root / "unmigrated.db"
        connection = sqlite3.connect(unmigrated)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
        before = unmigrated.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--database", str(unmigrated), "process-next"]), 0)
        self.assertEqual(unmigrated.read_bytes(), before)
        self.assertEqual(json.loads(output.getvalue())["next_stage"], "no_work")


if __name__ == "__main__":
    unittest.main()
