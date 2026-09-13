from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_first_orchestrator.decomposition import Criterion, FeatureContract
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.repository_snapshot import Evidence, ManifestEntry, RepositorySnapshot
from local_first_orchestrator.runtime_metrics import PlannerSizing, RuntimeMetricsStore
from local_first_orchestrator.states import CanonicalState


class RuntimeMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()

    def tearDown(self) -> None:
        self.ledger.close()

    def completed_ticket(self) -> str:
        ticket_id = self.ledger.create_ticket(
            title="metrics ticket",
            state=CanonicalState.DRAFT,
            contract={
                "objective": "bounded change",
                "criterion_ids": ["C1"],
                "primary_symbol": "app.py::run",
                "allowed_files": ["app.py"],
                "create_files": [],
                "new_test_files": ["test_app.py"],
                "forbidden_changes": ["unrelated behavior"],
                "patch_budget": {"max_files": 2, "max_changed_lines": 40},
                "verification": {"commands": [["python", "-m", "unittest"]]},
                "risk": "low",
                "review_required": True,
                "max_attempts": 2,
                "dependencies": [],
            },
        )
        with self.ledger._transaction() as conn:
            conn.execute("UPDATE tickets SET state='done' WHERE id=?", (ticket_id,))
            self.ledger._append_event(conn, entity_type="ticket", entity_id=ticket_id, event_type="state_transition", actor_id="controller", from_state="draft", to_state="done", payload={"fixture": True})
        return ticket_id

    def test_materialization_is_idempotent_and_append_only(self) -> None:
        ticket_id = self.completed_ticket()
        store = RuntimeMetricsStore(self.ledger)
        self.assertEqual(store.materialize_completed(), 1)
        self.assertEqual(store.materialize_completed(), 0)
        row = self.ledger.connection.execute("SELECT * FROM ticket_runtime_metrics WHERE ticket_id=?", (ticket_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["declared_files"], 2)
        self.assertGreater(row["context_tokens"], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE ticket_runtime_metrics SET attempts=2 WHERE ticket_id=?", (ticket_id,))

    def seed_metrics(self, *, accepted: int, attempts: int, count: int = 6) -> RuntimeMetricsStore:
        now = self.ledger._now()
        for index in range(count):
            ticket_id = self.ledger.create_ticket(title=f"m-{index}")
            self.ledger.connection.execute(
                """INSERT INTO ticket_runtime_metrics(ticket_id,feature_id,tranche_id,attempts,accepted,context_tokens,declared_files,implementation_seconds,review_seconds,completed_at,recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ticket_id, "F", "TR", attempts, accepted, 15000, 2, 3.0, 1.0, now + index, now + index),
            )
        return RuntimeMetricsStore(self.ledger)

    def test_recommendation_shrinks_after_rework_and_expands_after_strong_first_attempts(self) -> None:
        low = self.seed_metrics(accepted=0, attempts=2).recommendation()
        self.assertEqual((low.max_active_tickets, low.target_context_tokens, low.reason), (3, 16000, "reduce_scope_after_rework"))
        other = Ledger(self.root / "second.db"); other.migrate()
        try:
            now = other._now()
            for index in range(6):
                ticket_id = other.create_ticket(title=f"high-{index}")
                other.connection.execute(
                    """INSERT INTO ticket_runtime_metrics(ticket_id,feature_id,tranche_id,attempts,accepted,context_tokens,declared_files,implementation_seconds,review_seconds,completed_at,recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (ticket_id, "F", "TR", 1, 1, 15000, 2, 3.0, 1.0, now + index, now + index),
                )
            high = RuntimeMetricsStore(other).recommendation()
        finally:
            other.close()
        self.assertEqual((high.max_active_tickets, high.target_context_tokens, high.reason), (5, 24000, "expand_with_strong_first_attempt_rate"))

    def test_planner_packet_uses_bounded_adaptive_sizing(self) -> None:
        feature = FeatureContract("F", "title", "objective", (Criterion("C1", "works", "test"),), (), (), (), "rev")
        snapshot = RepositorySnapshot("repo", "a" * 40, (ManifestEntry("app.py", "source"),), (Evidence("app.py", "hash", "fixture", ("app.py::run",), 1),), 0)
        response = {
            "plan_version": 1,
            "feature_id": "F",
            "feature_contract_hash": feature.contract_hash,
            "repo_base_sha": snapshot.base_sha,
            "repo_snapshot_hash": snapshot.snapshot_hash,
            "architecture_decisions": ["bounded"],
            "criterion_coverage": {"C1": ["T1"]},
            "tranches": [{"id": "TR", "ordinal": 0, "objective": "o", "capabilities": ["c"], "criterion_ids": ["C1"], "microtickets": [{"ticket_id": "T1", "objective": "o", "criterion_ids": ["C1"], "primary_symbol": "app.py::run", "allowed_files": ["app.py"], "create_files": [], "new_test_files": [], "forbidden_changes": [], "patch_budget": {"max_files": 1, "max_changed_lines": 20}, "verification": {"commands": [["true"]]}, "risk": "low", "review_required": True, "max_attempts": 2, "dependencies": []}]}],
        }
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")
        with tempfile.TemporaryDirectory() as tmp:
            planner = LocalDecompositionPlanner(runner, cost_class="local", provider="p", model="m", profile="x", sizing_provider=lambda: PlannerSizing(6, 28000, 12, "fixture"))
            planner.propose(feature, snapshot, artifact_dir=Path(tmp))
            request = json.loads((Path(tmp) / "planner-request.json").read_text())
            provenance = json.loads((Path(tmp) / "planner-provenance.json").read_text())
        self.assertEqual(request["limits"]["active_tranche_max_tickets"], 6)
        self.assertEqual(request["limits"]["target_context_tokens"], 28000)
        self.assertEqual(provenance["adaptive_sizing"]["reason"], "fixture")


if __name__ == "__main__":
    unittest.main()
