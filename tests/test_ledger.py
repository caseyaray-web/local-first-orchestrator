from __future__ import annotations

import sqlite3
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.adapters import FakeBoardAdapter
from local_first_orchestrator.cli import main as cli_main
from local_first_orchestrator.config import OrchestratorConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState, InvalidTransition


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "orchestrator.db"
        self.db = Ledger(self.path)
        self.db.migrate()

    def tearDown(self) -> None:
        self.db.close()
        self.tempdir.cleanup()

    def create_ticket(self, status: CanonicalState = CanonicalState.DRAFT) -> str:
        return self.db.create_ticket(title="Bounded ticket", state=status)

    def test_allowed_transition_persists_an_immutable_event_atomically(self) -> None:
        ticket = self.create_ticket()
        self.db.transition(ticket, CanonicalState.READY_LOCAL, actor_id="test")

        stored = self.db.get_ticket(ticket)
        events = self.db.events_for(ticket)
        self.assertEqual(stored["state"], CanonicalState.READY_LOCAL.value)
        self.assertEqual(
            [(event["from_state"], event["to_state"]) for event in events],
            [(CanonicalState.DRAFT.value, CanonicalState.READY_LOCAL.value)],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute("UPDATE events SET to_state = 'done' WHERE id = ?", (events[0]["id"],))

    def test_forbidden_transition_does_not_change_ticket_or_append_event(self) -> None:
        ticket = self.create_ticket()
        with self.assertRaises(InvalidTransition):
            self.db.transition(ticket, CanonicalState.DONE)
        self.assertEqual(self.db.get_ticket(ticket)["state"], CanonicalState.DRAFT.value)
        self.assertEqual(self.db.events_for(ticket), [])

    def test_atomic_lease_is_exclusive_and_expired_claim_recovers(self) -> None:
        ticket = self.create_ticket(CanonicalState.READY_LOCAL)
        self.assertEqual(self.db.claim_ticket("worker-a", lease_seconds=1, now=100), ticket)
        self.assertIsNone(self.db.claim_ticket("worker-b", lease_seconds=1, now=100))
        self.assertEqual(self.db.recover_expired_leases(now=102), [ticket])
        self.assertEqual(self.db.get_ticket(ticket)["state"], CanonicalState.READY_LOCAL.value)
        self.assertEqual(self.db.claim_ticket("worker-b", lease_seconds=10, now=102), ticket)

    def test_stage_idempotency_is_restart_safe(self) -> None:
        ticket = self.create_ticket(CanonicalState.READY_LOCAL)
        self.assertTrue(self.db.record_stage(ticket, 1, "validate", "ticket:1:validate"))
        self.db.close()
        self.db = Ledger(self.path)
        self.db.migrate()
        self.assertFalse(self.db.record_stage(ticket, 1, "validate", "ticket:1:validate"))
        self.assertEqual(self.db.stage_count(ticket), 1)

    def test_pause_resume_and_status_survive_restart(self) -> None:
        self.create_ticket(CanonicalState.READY_LOCAL)
        self.db.pause("operator", reason="maintenance")
        self.assertTrue(self.db.status()["paused"])
        self.db.close()
        self.db = Ledger(self.path)
        self.db.migrate()
        self.assertTrue(self.db.status()["paused"])
        self.db.resume("operator", reason="maintenance complete")
        self.assertFalse(self.db.status()["paused"])

    def test_fake_ticket_projection_is_idempotent(self) -> None:
        ticket = self.create_ticket()
        board = FakeBoardAdapter()
        self.db.transition(ticket, CanonicalState.READY_LOCAL)
        self.assertTrue(self.db.project_ticket(ticket, board))
        self.assertEqual(board.states, {ticket: CanonicalState.READY_LOCAL})
        self.assertFalse(self.db.project_ticket(ticket, board))
        self.assertEqual(len(board.projections), 1)

    def test_non_generated_projection_uses_persisted_external_id(self) -> None:
        ticket = self.db.create_ticket(title="Imported", state=CanonicalState.DRAFT, external_id="external-42")
        board = FakeBoardAdapter()
        self.db.transition(ticket, CanonicalState.READY_LOCAL)
        self.assertTrue(self.db.project_ticket(ticket, board))
        self.assertEqual(board.states, {"external-42": CanonicalState.READY_LOCAL})
        row = self.db.connection.execute("SELECT external_task_id FROM board_projection_outbox WHERE ticket_id=?", (ticket,)).fetchone()
        self.assertEqual(row["external_task_id"], "external-42")

    def test_kanban_named_database_is_rejected_before_constructor_or_cli_mutates_it(self) -> None:
        kanban_path = Path(self.tempdir.name) / "kanban.db"

        with self.assertRaisesRegex(ValueError, "distinct"):
            Ledger(kanban_path)
        self.assertFalse(kanban_path.exists())

        with self.assertRaisesRegex(ValueError, "distinct"):
            cli_main(["--database", str(kanban_path), "migrate"])
        self.assertFalse(kanban_path.exists())

    def test_projection_outbox_retries_post_effect_failure_with_one_board_projection(self) -> None:
        ticket = self.create_ticket()
        board = FakeBoardAdapter(fail_after_effect_once=True)
        self.db.transition(ticket, CanonicalState.READY_LOCAL)

        with self.assertRaisesRegex(RuntimeError, "post-effect"):
            self.db.project_ticket(ticket, board)
        self.assertEqual(len(board.projections), 1)
        self.assertEqual(len(board.idempotency_keys), 1)

        self.db.close()
        self.db = Ledger(self.path)
        self.db.migrate()
        self.assertTrue(self.db.project_ticket(ticket, board))
        self.assertFalse(self.db.project_ticket(ticket, board))
        self.assertEqual(len(board.projections), 1)
        self.assertEqual(len(board.idempotency_keys), 1)
        acknowledgements = self.db.connection.execute(
            "SELECT COUNT(*) FROM board_projections WHERE ticket_id = ?", (ticket,)
        ).fetchone()[0]
        self.assertEqual(acknowledgements, 1)

    def test_reconcile_completed_duplicate_execution_terminalizes_generated_ticket(self) -> None:
        contract = {
            "objective": "same bounded work",
            "criterion_ids": ["A"],
            "primary_symbol": "app.py::run",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["no secrets"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 20, "exception_reason": None},
            "verification": {"commands": [["python", "-m", "compileall", "app.py"]], "working_directory": "."},
            "risk": "medium",
            "review_required": True,
            "max_attempts": 2,
            "dependencies": [],
            "new_test_files": [],
            "create_files": [],
        }
        planning = self.db.create_ticket(title="TK-1", state=CanonicalState.READY_LOCAL, contract=contract)
        execution_contract = dict(contract)
        execution_contract["verification"] = {**contract["verification"], "timeout_seconds": 60, "output_limit": 20000}
        execution = self.db.create_ticket(title="TK-1", state=CanonicalState.DONE, external_id="external-1", contract=execution_contract)
        with self.db._transaction() as conn:
            create_event = self.db._append_event(conn, entity_type="ticket", entity_id=planning, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": planning})
        self.db.enqueue_generated_create_projection(planning, create_event, {"ticket_id": planning}, "create-1")
        self.db.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=100,external_task_id='external-1' WHERE ticket_id=? AND event_id=? AND operation='create_microticket'", (planning, create_event))
        self.db.record_accepted_evidence(execution, "a" * 40, json.dumps({"candidate_fingerprint": "b" * 64}), "validated")
        with self.db._transaction() as conn:
            done_event = self.db._append_event(conn, entity_type="ticket", entity_id=execution, event_type="state_transition", actor_id="test", from_state="accepted", to_state="done", payload={"reason": "test"})
            self.db._enqueue_projection_bundle_in_transaction(conn, ticket_id=execution, event_id=done_event, evidence="done")
        self.db.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=101 WHERE ticket_id=? AND event_id=? AND operation='set_state'", (execution, done_event))
        self.db.bind_runtime(planning, "/repo", "1" * 40)
        self.db.connection.execute("INSERT INTO native_dependency_releases(ticket_id,graph_hash,child_external_id,parent_completion_hash,routing_authority_json,hermes_status,observed_at) VALUES (?,?,?,?,?,?,?)", (planning, "g" * 64, "external-1", "p" * 64, "{}", "ready", 100))
        self.db.pause("operator", reason="reconcile duplicate")

        result = self.db.reconcile_completed_duplicate_execution(planning, execution, operator_id="operator", reason="completed through imported execution")
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(self.db.get_ticket(planning)["state"], "done")
        copied = self.db.connection.execute("SELECT * FROM accepted_evidence WHERE ticket_id=?", (planning,)).fetchone()
        self.assertEqual(copied["accepted_commit_sha"], "a" * 40)
        self.assertEqual(json.loads(copied["diff_summary"])["reconciled_execution_ticket_id"], execution)
        pending_done = self.db.connection.execute("SELECT * FROM board_projection_outbox WHERE ticket_id=? AND operation='set_state' AND state='done'", (planning,)).fetchone()
        self.assertEqual(pending_done["external_task_id"], "external-1")
        self.assertIsNone(self.db.native_dependency_release_migration_required())
        replay = self.db.reconcile_completed_duplicate_execution(planning, execution, operator_id="operator", reason="completed through imported execution")
        self.assertEqual(replay["status"], "already_reconciled")

    def test_reconcile_completed_duplicate_execution_rejects_explicit_verification_limit_drift(self) -> None:
        contract = {
            "objective": "same bounded work",
            "criterion_ids": ["A"],
            "primary_symbol": "app.py::run",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["no secrets"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 20, "exception_reason": None},
            "verification": {"commands": [["python", "-m", "compileall", "app.py"]], "working_directory": ".", "timeout_seconds": 5, "output_limit": 1024},
            "risk": "medium",
            "review_required": True,
            "max_attempts": 2,
            "dependencies": [],
            "new_test_files": [],
            "create_files": [],
        }
        planning = self.db.create_ticket(title="TK-limit", state=CanonicalState.READY_LOCAL, contract=contract)
        execution_contract = dict(contract)
        execution_contract["verification"] = {**contract["verification"], "timeout_seconds": 300, "output_limit": 1_000_000}
        execution = self.db.create_ticket(title="TK-limit", state=CanonicalState.DONE, external_id="external-limit", contract=execution_contract)
        with self.db._transaction() as conn:
            create_event = self.db._append_event(conn, entity_type="ticket", entity_id=planning, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": planning})
        self.db.enqueue_generated_create_projection(planning, create_event, {"ticket_id": planning}, "create-limit")
        self.db.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=100,external_task_id='external-limit' WHERE ticket_id=? AND event_id=? AND operation='create_microticket'", (planning, create_event))
        self.db.record_accepted_evidence(execution, "a" * 40, json.dumps({"candidate_fingerprint": "b" * 64}), "validated")
        with self.db._transaction() as conn:
            done_event = self.db._append_event(conn, entity_type="ticket", entity_id=execution, event_type="state_transition", actor_id="test", from_state="accepted", to_state="done", payload={"reason": "test"})
            self.db._enqueue_projection_bundle_in_transaction(conn, ticket_id=execution, event_id=done_event, evidence="done")
        self.db.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=101 WHERE ticket_id=? AND event_id=? AND operation='set_state'", (execution, done_event))
        self.db.pause("operator", reason="reconcile duplicate")

        with self.assertRaisesRegex(ValueError, "verification_json"):
            self.db.reconcile_completed_duplicate_execution(planning, execution, operator_id="operator", reason="must reject verification limit drift")

    def test_config_requires_distinct_ledger_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "required"):
            OrchestratorConfig.from_mapping({})
        with self.assertRaisesRegex(ValueError, "distinct"):
            OrchestratorConfig.from_mapping({"database": "/home/u/.hermes/kanban.db"})
        config = OrchestratorConfig.from_mapping({"database": str(self.path)})
        self.assertEqual(config.database, self.path)


if __name__ == "__main__":
    unittest.main()
