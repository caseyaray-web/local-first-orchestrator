from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


class ReleaseRoutingEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ticket = self.ledger.create_ticket(title="root", state=CanonicalState.DRAFT, external_id="external-root")
        self.ledger.bind_runtime(self.ticket, str(self.root), "a" * 40)
        with self.ledger._transaction() as conn:
            event_id = self.ledger._append_event(conn, entity_type="ticket", entity_id=self.ticket, event_type="generated_microticket_created", actor_id="test", to_state="draft", payload={"ticket_id": self.ticket})
        self.ledger.enqueue_generated_create_projection(self.ticket, event_id, {"ticket_id": self.ticket}, "create-root")
        self.ledger.connection.execute("UPDATE board_projection_outbox SET acknowledged_at=100,external_task_id='external-root' WHERE ticket_id=? AND event_id=? AND operation='create_microticket'", (self.ticket, event_id))
        with self.ledger._transaction() as conn:
            identity = self.ledger._native_dependency_graph_identity(conn, self.ticket)
            conn.execute("INSERT INTO native_dependency_graphs(ticket_id,child_external_id,local_dependency_ids_json,parent_external_ids_json,graph_hash,verified_at) VALUES (?,?,?,?,?,?)", (self.ticket, "external-root", "[]", "[]", identity["graph_hash"], 100))

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def _claim(self):
        claim = self.ledger.claim_next_scheduler_native_dependency_release("owner", lease_seconds=30, now=100, implementation_profile="impl", canonical_repository=str(self.root))
        assert claim is not None
        self.ledger.begin_scheduler_claim_effect(claim["claim_id"], "owner", now=100)
        identity = json.loads(claim["candidate_identity_json"])
        return claim, identity

    def test_missing_or_forged_routing_authority_is_rejected_and_exact_replay_is_idempotent(self) -> None:
        claim, identity = self._claim()
        base = {"ticket_id": self.ticket, "candidate_identity": identity, "actual_parent_external_ids": [], "hermes_status": "ready"}
        with self.assertRaisesRegex(RuntimeError, "routing authority evidence drift"):
            self.ledger.apply_scheduler_native_dependency_release_effect(claim["claim_id"], "owner", base, now=100, implementation_profile="impl", canonical_repository=str(self.root))
        forged = {**base, "routing_authority": {"profile": "forged", "canonical_repository": str(self.root)}}
        with self.assertRaisesRegex(RuntimeError, "routing authority evidence drift"):
            self.ledger.apply_scheduler_native_dependency_release_effect(claim["claim_id"], "owner", forged, now=100, implementation_profile="impl", canonical_repository=str(self.root))
        exact = {**base, "routing_authority": identity["routing_authority"]}
        first = self.ledger.apply_scheduler_native_dependency_release_effect(claim["claim_id"], "owner", exact, now=100, implementation_profile="impl", canonical_repository=str(self.root))
        replay = self.ledger.apply_scheduler_native_dependency_release_effect(claim["claim_id"], "owner", exact, now=100, implementation_profile="impl", canonical_repository=str(self.root))
        self.assertEqual(first["result_json"], replay["result_json"])
        evidence = self.ledger.native_dependency_release(self.ticket)
        assert evidence is not None
        self.assertEqual(json.loads(evidence["routing_authority_json"]), identity["routing_authority"])


if __name__ == "__main__":
    unittest.main()
