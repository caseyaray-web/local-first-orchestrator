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
from local_first_orchestrator.execution_handoff import HANDOFF_MARKER
from local_first_orchestrator.hermes_board import ExternalTicket
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


class NativeBoard(Board):
    def __init__(self) -> None:
        super().__init__()
        self.tasks: dict[str, dict[str, object]] = {}
        self.link_calls: list[tuple[str, str]] = []
        self.fail_after_link_once = False

    def add_task(self, task_id: str, *, status: str, body: str = "") -> None:
        self.tasks[task_id] = {"status": status, "body": body, "parents": set(), "children": set()}

    def get_task(self, task_id: str) -> ExternalTicket:
        row = self.tasks[task_id]
        return ExternalTicket(
            task_id,
            task_id,
            str(row.get("body") or ""),
            str(row["status"]),
            None,
            tuple(sorted(row["parents"])),
            tuple(sorted(row["children"])),
        )

    def link_dependency(self, parent_task_id: str, child_task_id: str) -> None:
        self.link_calls.append((parent_task_id, child_task_id))
        self.tasks[parent_task_id]["children"].add(child_task_id)
        self.tasks[child_task_id]["parents"].add(parent_task_id)
        self._recompute(child_task_id)
        if self.fail_after_link_once:
            self.fail_after_link_once = False
            raise RuntimeError("simulated link transport loss")

    def set_state(self, ticket_id, state, *, idempotency_key):
        super().set_state(ticket_id, state, idempotency_key=idempotency_key)
        if ticket_id not in self.tasks:
            return
        if state == CanonicalState.DONE:
            self.tasks[ticket_id]["status"] = "done"
            for child in tuple(self.tasks[ticket_id]["children"]):
                self._recompute(str(child))
        elif state == CanonicalState.READY_LOCAL:
            parents = self.tasks[ticket_id]["parents"]
            self.tasks[ticket_id]["status"] = "ready" if not parents else (
                "ready" if all(self.tasks[str(parent)]["status"] == "done" for parent in parents) else "todo"
            )

    def _recompute(self, child_task_id: str) -> None:
        if self.tasks[child_task_id]["status"] == "blocked":
            return
        parents = self.tasks[child_task_id]["parents"]
        self.tasks[child_task_id]["status"] = (
            "ready" if parents and all(self.tasks[str(parent)]["status"] == "done" for parent in parents) else "todo"
        )


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

    def test_native_dependency_graph_and_release_use_hermes_as_readiness_authority(self) -> None:
        board = NativeBoard()
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        board.add_task("external-parent", status="ready")
        board.add_task("external-child", status="ready")
        scheduler = ProcessNextScheduler(self.ledger, board, worker_id="native", lease_seconds=30, clock=lambda: 100)

        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id, preview.would_write_board), ("native_dependency_graph", child, True))
        graph_result = scheduler.process_next()
        self.assertEqual((graph_result.stage, graph_result.ticket_id), ("native_dependency_graph", child))
        self.assertEqual(board.link_calls, [("external-parent", "external-child")])
        self.assertEqual(board.get_task("external-child").parents, ("external-parent",))
        self.assertEqual(board.get_task("external-child").status, "todo")
        graph = self.ledger.native_dependency_graph(child)
        self.assertEqual(json.loads(graph["local_dependency_ids_json"]), [parent])
        self.assertEqual(json.loads(graph["parent_external_ids_json"]), ["external-parent"])
        self.assertEqual(self.ledger.get_ticket(child)["state"], "draft")

        self.ledger.transition(parent, CanonicalState.DONE)
        self.ledger.record_accepted_evidence(parent, "a" * 40, "accepted", "validated")
        self.assertEqual(scheduler.process_next().stage, "state_projection")
        self.assertEqual(board.get_task("external-parent").status, "done")
        self.assertEqual(board.get_task("external-child").status, "ready")
        self.assertEqual(scheduler.process_next().stage, "evidence_comment")
        release_preview = preview_next(self.ledger, now=100)
        self.assertEqual((release_preview.next_stage, release_preview.ticket_id), ("native_dependency_release", child))
        release_result = scheduler.process_next()
        self.assertEqual((release_result.stage, release_result.ticket_id), ("native_dependency_release", child))
        release = self.ledger.native_dependency_release(child)
        self.assertEqual(release["hermes_status"], "ready")
        self.assertEqual(release["graph_hash"], graph["graph_hash"])
        self.assertEqual(self.ledger.get_ticket(child)["state"], "ready_local")
        projection = self.ledger.plan_projection(child)
        self.assertIsNotNone(projection)
        self.assertIsNotNone(projection["state"]["acknowledged_at"])
        self.assertEqual(projection["comment"]["status"], "pending")
        self.assertIsNone(self.ledger.claim_next_scheduler_readiness("legacy", lease_seconds=30, now=100))

    def test_handoff_dependent_is_unblocked_only_after_graph_converges(self) -> None:
        board = NativeBoard()
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        board.add_task("external-parent", status="ready", body=HANDOFF_MARKER)
        board.add_task("external-child", status="blocked", body=HANDOFF_MARKER)
        scheduler = ProcessNextScheduler(self.ledger, board, worker_id="native", lease_seconds=30, clock=lambda: 100)

        result = scheduler.process_next()

        self.assertEqual((result.stage, result.ticket_id), ("native_dependency_graph", child))
        self.assertEqual(board.get_task("external-child").parents, ("external-parent",))
        self.assertEqual(board.get_task("external-child").status, "todo")
        self.assertEqual(board.states[-1][0:2], ("external-child", "ready_local"))
        self.assertEqual(self.ledger.get_ticket(child)["state"], "draft")

    def test_generated_activation_runner_uses_dependency_readiness_slot(self) -> None:
        calls: list[str] = []
        scheduler = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="activation",
            lease_seconds=30,
            clock=lambda: 100,
            generated_activation_runner=lambda: calls.append("run") or {"ticket_id": "generated-1", "status": "activated_waiting"},
        )

        result = scheduler.process_next()

        self.assertEqual((result.stage, result.status, result.ticket_id), ("dependency_readiness", "activated_waiting", "generated-1"))
        self.assertEqual(calls, ["run"])

    def test_native_dependency_graph_replays_after_link_transport_loss_without_duplicate_edge(self) -> None:
        board = NativeBoard()
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        board.add_task("external-parent", status="ready")
        board.add_task("external-child", status="ready")
        board.fail_after_link_once = True
        first = ProcessNextScheduler(self.ledger, board, worker_id="native-a", lease_seconds=30, clock=lambda: 100)
        with self.assertRaisesRegex(RuntimeError, "simulated link transport loss"):
            first.process_next()
        self.assertEqual(board.get_task("external-child").parents, ("external-parent",))
        self.assertEqual(board.link_calls, [("external-parent", "external-child")])
        self.assertIsNone(self.ledger.native_dependency_graph(child))

        replay = ProcessNextScheduler(self.ledger, board, worker_id="native-b", lease_seconds=30, clock=lambda: 131)
        result = replay.process_next()
        self.assertEqual((result.stage, result.ticket_id), ("native_dependency_graph", child))
        self.assertEqual(board.link_calls, [("external-parent", "external-child")])
        self.assertIsNotNone(self.ledger.native_dependency_graph(child))

    def test_native_dependency_release_reclaims_started_read_and_rechecks_hermes(self) -> None:
        board = NativeBoard()
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        board.add_task("external-parent", status="ready")
        board.add_task("external-child", status="ready")
        scheduler = ProcessNextScheduler(self.ledger, board, worker_id="native", lease_seconds=30, clock=lambda: 100)
        self.assertEqual(scheduler.process_next().stage, "native_dependency_graph")
        self.ledger.transition(parent, CanonicalState.DONE)
        self.ledger.record_accepted_evidence(parent, "a" * 40, "accepted", "validated")
        self.assertEqual(scheduler.process_next().stage, "state_projection")
        self.assertEqual(scheduler.process_next().stage, "evidence_comment")
        claim = self.ledger.claim_next_scheduler_native_dependency_release("crashed", lease_seconds=1, now=101)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        self.assertIsNone(self.ledger.native_dependency_release(child))
        resumed = ProcessNextScheduler(self.ledger, board, worker_id="native-recovery", lease_seconds=30, clock=lambda: 103)
        result = resumed.process_next()
        self.assertEqual((result.stage, result.ticket_id), ("native_dependency_release", child))
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        self.assertEqual(self.ledger.native_dependency_release(child)["hermes_status"], "ready")
        self.assertEqual(board.link_calls, [("external-parent", "external-child")])
        self.assertEqual(self.ledger.get_ticket(child)["state"], "ready_local")

    def test_native_dependency_graph_stops_on_extra_hermes_parent(self) -> None:
        board = NativeBoard()
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        board.add_task("external-parent", status="ready")
        board.add_task("external-extra", status="ready")
        board.add_task("external-child", status="todo")
        board.tasks["external-extra"]["children"].add("external-child")
        board.tasks["external-child"]["parents"].add("external-extra")
        scheduler = ProcessNextScheduler(self.ledger, board, worker_id="native", lease_seconds=30, clock=lambda: 100)
        with self.assertRaisesRegex(RuntimeError, "unexpected parents"):
            scheduler.process_next()
        self.assertEqual(board.link_calls, [])
        self.assertIsNone(self.ledger.native_dependency_graph(child))

    def test_legacy_scheduler_readiness_never_admits_dependent_ticket(self) -> None:
        parent = self.ticket("parent", state=CanonicalState.ACCEPTED)
        child = self.ticket("child", dependencies=(parent,))
        self.ledger.transition(parent, CanonicalState.DONE)
        self.ledger.record_accepted_evidence(parent, "a" * 40, "accepted", "validated")
        self.assertIsNone(self.ledger.claim_next_scheduler_readiness("worker", lease_seconds=30, now=100))
        self.assertEqual(self.ledger.get_ticket(child)["state"], "draft")

    def test_claim_skips_malformed_legacy_dependency_json(self) -> None:
        malformed = self.ticket("malformed")
        self.ledger.connection.execute(
            "UPDATE tickets SET dependencies_json='not-json' WHERE id=?", (malformed,)
        )
        ready = self.ticket("ready")

        claim = self.ledger.claim_next_scheduler_readiness("worker", lease_seconds=30, now=100)

        self.assertEqual(claim["ticket_id"], ready)
        self.assertEqual(self.ledger.get_ticket(malformed)["state"], "draft")

    def test_expired_claim_replays_completed_effect_without_duplicate_transition(self) -> None:
        ticket = self.ticket("ready")
        first = self.ledger.claim_next_scheduler_readiness("worker-a", lease_seconds=1, now=100)
        self.assertEqual(first["ticket_id"], ticket)
        self.ledger.begin_scheduler_claim_effect(first["claim_id"], "worker-a", now=100)
        applied = self.ledger.apply_scheduler_readiness_effect(first["claim_id"], "worker-a", now=100)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        self.ledger.close()

        self.ledger = Ledger(self.database)
        self.ledger.migrate()
        replay = self.ledger.claim_next_scheduler_readiness("worker-b", lease_seconds=10, now=102)
        self.assertEqual(replay["claim_id"], first["claim_id"])
        reapplied = self.ledger.apply_scheduler_readiness_effect(first["claim_id"], "worker-b", now=102)
        result = json.loads(reapplied["result_json"])
        self.ledger.complete_scheduler_claim(first["claim_id"], "worker-b", result, now=102)

        transitions = [event for event in self.ledger.events_for(ticket) if event["event_type"] == "state_transition"]
        self.assertEqual(len(transitions), 1)
        row = self.ledger.scheduler_claim(first["claim_id"])
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["side_effect_started_at"])
        self.assertIsNotNone(row["side_effect_completed_at"])
        self.assertIsNotNone(row["finalized_at"])
        self.assertEqual(row["attempt_count"], 2)

    def test_readiness_effect_failure_rolls_back_transition_but_preserves_started_marker(self) -> None:
        ticket = self.ticket("atomic")
        claim = self.ledger.claim_next_scheduler_readiness("worker-a", lease_seconds=1, now=100)
        self.ledger.begin_scheduler_claim_effect(claim["claim_id"], "worker-a", now=100)
        self.ledger.failure_injector = lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "after_event_creation" else None

        with self.assertRaisesRegex(RuntimeError, "after_event_creation"):
            self.ledger.apply_scheduler_readiness_effect(claim["claim_id"], "worker-a", now=100)

        row = self.ledger.scheduler_claim(claim["claim_id"])
        self.assertIsNotNone(row["side_effect_started_at"])
        self.assertIsNone(row["side_effect_completed_at"])
        self.assertIsNone(row["finalized_at"])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "draft")
        self.assertFalse([e for e in self.ledger.events_for(ticket) if e["event_type"] == "state_transition"])

        self.ledger.failure_injector = None
        replay = self.ledger.claim_next_scheduler_readiness("worker-b", lease_seconds=10, now=102)
        applied = self.ledger.apply_scheduler_readiness_effect(replay["claim_id"], "worker-b", now=102)
        result = json.loads(applied["result_json"])
        self.ledger.complete_scheduler_claim(replay["claim_id"], "worker-b", result, now=102)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "ready_local")

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

    def test_preview_reports_ready_local_implementation_without_mutation(self) -> None:
        ticket = self.ticket("implement", state=CanonicalState.READY_LOCAL)
        before_events = self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        preview = preview_next(self.ledger, now=100)

        self.assertEqual((preview.status, preview.next_stage, preview.ticket_id), ("dry_run", "implementation", ticket))
        self.assertTrue(preview.would_execute)
        self.assertFalse(preview.would_write_board)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM scheduler_stage_claims").fetchone()[0], 0)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_events)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"], "ready_local")

    def test_completed_implementation_effect_finalizes_after_restart_without_runner(self) -> None:
        ticket = self.ticket("implement", state=CanonicalState.READY_LOCAL)
        claim = self.ledger.claim_next_scheduler_implementation("worker-a", lease_seconds=1, now=100)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "worker-a", now=100)
        self.ledger.ensure_attempt(ticket, 1)
        self.ledger.record_model_stage(
            ticket,
            1,
            "implementation",
            purpose="implementation",
            adapter="fixture",
            request_hash="request",
            response_artifact=str(self.root / "artifact.json"),
            worktree_path=str(self.root / "worktree"),
            base_sha="a" * 40,
            diff_hash="diff",
        )
        result = {"ticket_id": ticket, "attempt_number": 1, "implementation_artifact": str(self.root / "artifact.json"), "diff_hash": "diff", "replayed": False}
        self.ledger.complete_scheduler_implementation_effect(claim_id, "worker-a", result, now=100)
        calls: list[str] = []

        resumed = ProcessNextScheduler(
            self.ledger,
            self.board,
            worker_id="worker-b",
            lease_seconds=30,
            clock=lambda: 102,
            implementation_runner=lambda ticket_id: calls.append(ticket_id) or (_ for _ in ()).throw(AssertionError("runner must not be called")),
        ).process_next()

        self.assertEqual((resumed.stage, resumed.status, resumed.ticket_id), ("implementation", "completed", ticket))
        self.assertEqual(calls, [])
        final = self.ledger.scheduler_claim(claim_id)
        self.assertEqual(final["status"], "completed")
        self.assertIsNotNone(final["finalized_at"])

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
