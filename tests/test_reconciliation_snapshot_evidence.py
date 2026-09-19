from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class ReconciliationSnapshotEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ledger.pause("operator", reason="reconciliation evidence test")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def snapshot(self, *, ticket_id: str, status: str, parents: tuple[str, ...] = (), label: str = "snapshot") -> tuple[str, str]:
        payload = {
            "task": {"id": ticket_id, "title": ticket_id, "body": "", "status": status, "workspace_path": None},
            "latest_summary": None,
            "parents": list(parents),
            "children": [],
            "comments": [],
            "events": [],
            "runs": [],
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        directory = self.root / "hermes-reconciliation-evidence"
        directory.mkdir(exist_ok=True)
        path = directory / f"{label}.json"
        path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def generated_projection(self, ticket_id: str, external_id: str) -> int:
        with self.ledger._transaction() as conn:
            event_id = self.ledger._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket_id,
                event_type="generated_microticket_created",
                actor_id="test",
                to_state="draft",
                payload={"ticket_id": ticket_id},
            )
            conn.execute(
                "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, event_id, "draft", "{}", f"create:{ticket_id}", 1, "create_microticket", external_id, 1),
            )
        return event_id

    def test_done_projection_requires_canonical_snapshot_evidence(self) -> None:
        ticket = self.ledger.create_ticket(title="done", state=CanonicalState.DONE)
        self.ledger.record_accepted_evidence(ticket, "a" * 40, "accepted", "validated")
        self.generated_projection(ticket, "original")
        with self.ledger._transaction() as conn:
            stale_event = self.ledger._append_event(
                conn,
                entity_type="ticket",
                entity_id=ticket,
                event_type="state_transition",
                actor_id="controller",
                from_state="accepted",
                to_state="done",
                payload={},
            )
            conn.execute(
                "INSERT INTO board_projection_outbox(ticket_id,event_id,state,payload_json,idempotency_key,queued_at,operation,external_task_id) VALUES (?,?,?,?,?,?,?,?)",
                (ticket, stale_event, "done", "{}", f"done:{ticket}", 2, "set_state", "repair-child"),
            )

        ready_path, ready_sha = self.snapshot(ticket_id="original", status="ready", label="ready")
        with self.assertRaisesRegex(ValueError, "does not prove original task completion"):
            self.ledger.reconcile_done_projection_to_original_generated_task(
                ticket,
                stale_event_id=stale_event,
                original_external_task_id="original",
                snapshot_artifact_path=ready_path,
                snapshot_artifact_sha256=ready_sha,
                operator_id="operator",
                reason="test",
            )
        done_path, done_sha = self.snapshot(ticket_id="original", status="done", label="done")
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            self.ledger.reconcile_done_projection_to_original_generated_task(
                ticket,
                stale_event_id=stale_event,
                original_external_task_id="original",
                snapshot_artifact_path=done_path,
                snapshot_artifact_sha256="0" * 64,
                operator_id="operator",
                reason="test",
            )
        result = self.ledger.reconcile_done_projection_to_original_generated_task(
            ticket,
            stale_event_id=stale_event,
            original_external_task_id="original",
            snapshot_artifact_path=done_path,
            snapshot_artifact_sha256=done_sha,
            operator_id="operator",
            reason="test",
        )
        self.assertEqual(result["status"], "acknowledged")
        replacement = self.ledger.connection.execute(
            "SELECT * FROM board_projection_outbox WHERE ticket_id=? AND event_id=?",
            (ticket, result["replacement_event_id"]),
        ).fetchone()
        self.assertEqual(replacement["external_task_id"], "original")
        self.assertIsNotNone(replacement["acknowledged_at"])

    def test_completed_repair_graph_rejects_parent_drift_and_replays_at_new_time(self) -> None:
        cause = self.ledger.create_ticket(title="cause", state=CanonicalState.DONE)
        downstream = self.ledger.create_ticket(
            title="downstream",
            state=CanonicalState.DRAFT,
            contract={"dependencies": [cause]},
        )
        self.ledger.record_accepted_evidence(cause, "a" * 40, "accepted", "validated")
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,accepted_commit_sha,created_at) VALUES (?,?,?,?,?)",
            (cause, 2, "b" * 40, "a" * 40, 1),
        )
        self.generated_projection(cause, "original-parent")
        graph_hash = self.ledger._native_dependency_graph_hash(
            downstream,
            "child",
            (cause,),
            ("repair-parent",),
        )
        self.ledger.connection.execute(
            "INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,?)",
            (downstream, "child", json.dumps([cause]), json.dumps(["repair-parent"]), graph_hash, 1),
        )
        predecessor_path, predecessor_sha = self.snapshot(ticket_id="repair-parent", status="archived", label="predecessor")
        wrong_child_path, wrong_child_sha = self.snapshot(ticket_id="child", status="ready", parents=("wrong-parent",), label="child-wrong")
        with self.assertRaisesRegex(ValueError, "observed Hermes parents drift"):
            self.ledger.reconcile_completed_repair_dependency_graph(
                downstream_ticket_id=downstream,
                cause_ticket_id=cause,
                cause_attempt_number=2,
                predecessor_external_task_id="repair-parent",
                replacement_external_task_id="original-parent",
                predecessor_snapshot_artifact_path=predecessor_path,
                predecessor_snapshot_artifact_sha256=predecessor_sha,
                child_snapshot_artifact_path=wrong_child_path,
                child_snapshot_artifact_sha256=wrong_child_sha,
                operator_id="operator",
                reason="restore original parent",
                now=10,
            )
        child_path, child_sha = self.snapshot(ticket_id="child", status="ready", parents=("original-parent",), label="child")
        first = self.ledger.reconcile_completed_repair_dependency_graph(
            downstream_ticket_id=downstream,
            cause_ticket_id=cause,
            cause_attempt_number=2,
            predecessor_external_task_id="repair-parent",
            replacement_external_task_id="original-parent",
            predecessor_snapshot_artifact_path=predecessor_path,
            predecessor_snapshot_artifact_sha256=predecessor_sha,
            child_snapshot_artifact_path=child_path,
            child_snapshot_artifact_sha256=child_sha,
            operator_id="operator",
            reason="restore original parent",
            now=10,
        )
        replay = self.ledger.reconcile_completed_repair_dependency_graph(
            downstream_ticket_id=downstream,
            cause_ticket_id=cause,
            cause_attempt_number=2,
            predecessor_external_task_id="repair-parent",
            replacement_external_task_id="original-parent",
            predecessor_snapshot_artifact_path=predecessor_path,
            predecessor_snapshot_artifact_sha256=predecessor_sha,
            child_snapshot_artifact_path=child_path,
            child_snapshot_artifact_sha256=child_sha,
            operator_id="operator",
            reason="restore original parent",
            now=20,
        )
        self.assertEqual(first["revision_id"], replay["revision_id"])
        self.assertEqual(first["created_at"], 10)
        self.assertEqual(replay["created_at"], 10)


if __name__ == "__main__":
    unittest.main()
