from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import unittest

from local_first_orchestrator.tranche_completion import completion_evidence
from tests.test_supplemental_corrections import SupplementalCorrectionTests


class TrancheCompletionRecheckTests(SupplementalCorrectionTests):
    def record_h1(self):
        evidence = {
            "tranche_id": "T", "root_planning_sha": self.base,
            "final_integration_sha": self.accepted_map["A"],
            "accepted_ticket_ids_json": json.dumps(["A"], separators=(",", ":")),
            "accepted_commit_shas_json": json.dumps([self.accepted_map["A"]], separators=(",", ":")),
            "evidence_hash": "h1-evidence",
        }
        self.ledger.record_tranche_completion(evidence)
        return self.ledger.tranche_completion("T")

    def recheck(self, generation=None, previous_generation=None, previous_hash=None, plan_ids=None, ticket_ids=None, commits=None, head=None, unresolved=0):
        return self.ledger.record_tranche_completion_recheck("T", self.repo)

    def accept_correction(self, ticket_id):
        self.git("commit", "--allow-empty", "-m", f"accepted correction {ticket_id}")
        head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", head)
        self.ledger.connection.execute("UPDATE tickets SET state='done' WHERE id=?", (ticket_id,))
        self.ledger.record_accepted_evidence(ticket_id, head, "accepted", "validated")
        return head

    def test_open_correction_preserves_immutable_h1_and_blocks_successful_recheck(self):
        h1 = self.record_h1()
        plan = self.service().create_plan(self.spec("cycle-one"))
        correction = self.service().materialize(plan.correction_plan_id)
        lifecycle = self.service().lifecycle_status("T")
        self.assertEqual(lifecycle["review_status"], "open_corrections")
        self.assertEqual(lifecycle["unresolved_corrections"], 1)
        with self.assertRaises(ValueError):
            self.recheck(1, 0, h1["evidence_hash"], [plan.correction_plan_id], [correction.ticket_id], [], self.accepted_map["A"], unresolved=1)
        self.assertEqual(self.ledger.tranche_completion("T"), h1)
        self.assertEqual(self.ledger.tranche_completion_rechecks("T"), [])

    def test_accepted_integrated_correction_creates_idempotent_h2_recheck(self):
        h1 = self.record_h1()
        plan = self.service().create_plan(self.spec("cycle-one"))
        correction = self.service().materialize(plan.correction_plan_id)
        head2 = self.accept_correction(correction.ticket_id)
        row = self.recheck(1, 0, h1["evidence_hash"], [plan.correction_plan_id], [correction.ticket_id], [head2], head2)
        replay = self.recheck(1, 0, h1["evidence_hash"], [plan.correction_plan_id], [correction.ticket_id], [head2], head2)
        self.assertEqual(row["id"], replay["id"])
        self.assertEqual(row["generation"], 1)
        self.assertEqual(row["current_integration_sha"], head2)
        self.assertEqual(self.ledger.tranche_completion("T"), h1)
        self.assertEqual(self.service().lifecycle_status("T")["latest_completion"]["generation"], 1)

    def test_recheck_api_rejects_caller_supplied_evidence_claims(self):
        self.record_h1()
        with self.assertRaises(TypeError):
            self.ledger.record_tranche_completion_recheck("T", self.repo, **{"correction_plan_ids": []})  # type: ignore[call-arg]

    def test_missing_evidence_and_nonterminal_ticket_fail_closed(self):
        self.record_h1(); plan = self.service().create_plan(self.spec("missing-evidence")); correction = self.service().materialize(plan.correction_plan_id)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.ledger.record_tranche_completion_recheck("T", self.repo)
        self.accept_correction(correction.ticket_id)
        self.ledger.connection.execute("DELETE FROM accepted_evidence WHERE ticket_id=?", (correction.ticket_id,))
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.ledger.record_tranche_completion_recheck("T", self.repo)

    def test_stale_head_and_out_of_lineage_commit_fail_closed(self):
        self.record_h1(); plan = self.service().create_plan(self.spec("lineage")); correction = self.service().materialize(plan.correction_plan_id)
        head2 = self.accept_correction(correction.ticket_id)
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", self.base)
        with self.assertRaisesRegex(ValueError, "outside integration lineage"):
            self.ledger.record_tranche_completion_recheck("T", self.repo)
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", head2)
        self.git("checkout", "-b", "unintegrated-side", self.base); self.git("commit", "--allow-empty", "-m", "unintegrated correction evidence")
        side = self.git("rev-parse", "HEAD"); self.git("checkout", "main")
        self.ledger.record_accepted_evidence(correction.ticket_id, side, "accepted", "tampered")
        with self.assertRaisesRegex(ValueError, "outside integration lineage"):
            self.ledger.record_tranche_completion_recheck("T", self.repo)

    def test_second_correction_cycle_appends_h3_without_rewriting_h1_or_h2(self):
        h1 = self.record_h1()
        first_plan = self.service().create_plan(self.spec("cycle-one"))
        first = self.service().materialize(first_plan.correction_plan_id)
        head2 = self.accept_correction(first.ticket_id)
        h2 = self.recheck(1, 0, h1["evidence_hash"], [first_plan.correction_plan_id], [first.ticket_id], [head2], head2)
        second_plan = self.service().create_plan(self.spec("cycle-two"))
        second = self.service().materialize(second_plan.correction_plan_id)
        self.assertEqual(self.service().lifecycle_status("T")["unresolved_corrections"], 1)
        head3 = self.accept_correction(second.ticket_id)
        h3 = self.recheck(2, 1, h2["evidence_hash"], [second_plan.correction_plan_id], [second.ticket_id], [head3], head3)
        generations = self.ledger.tranche_completion_rechecks("T")
        self.assertEqual([row["generation"] for row in generations], [1, 2])
        self.assertEqual(generations[0]["evidence_hash"], h2["evidence_hash"])
        self.assertEqual(generations[1]["evidence_hash"], h3["evidence_hash"])
        self.assertEqual(json.loads(generations[1]["correction_plan_ids_json"]), [first_plan.correction_plan_id, second_plan.correction_plan_id])
        self.assertEqual(json.loads(generations[1]["accepted_ticket_ids_json"]), [first.ticket_id, second.ticket_id])
        self.assertEqual(json.loads(generations[1]["accepted_commit_shas_json"]), [h2["current_integration_sha"], h3["current_integration_sha"]])
        self.assertEqual(self.ledger.tranche_completion("T"), h1)
        self.assertEqual(self.service().lifecycle_status("T")["latest_completion"]["generation"], 2)

    def test_reopen_preserves_all_generations_and_unresolved_state(self):
        h1 = self.record_h1()
        first_plan = self.service().create_plan(self.spec("cycle-one"))
        first = self.service().materialize(first_plan.correction_plan_id)
        head2 = self.accept_correction(first.ticket_id)
        self.recheck(1, 0, h1["evidence_hash"], [first_plan.correction_plan_id], [first.ticket_id], [head2], head2)
        second_plan = self.service().create_plan(self.spec("cycle-two"))
        self.service().materialize(second_plan.correction_plan_id)
        self.ledger.close()
        from local_first_orchestrator.ledger import Ledger
        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.assertEqual([row["generation"] for row in self.ledger.tranche_completion_rechecks("T")], [1])
        self.assertEqual(self.service().lifecycle_status("T")["unresolved_corrections"], 1)
        self.assertEqual(self.ledger.tranche_completion("T")["evidence_hash"], h1["evidence_hash"])
    def test_h1_and_recheck_rows_are_database_immutable(self):
        h1 = self.record_h1()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE tranche_completion_evidence SET evidence_hash='changed' WHERE tranche_id='T'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("DELETE FROM tranche_completion_evidence WHERE tranche_id='T'")

        plan = self.service().create_plan(self.spec("immutable"))
        correction = self.service().materialize(plan.correction_plan_id)
        head = self.accept_correction(correction.ticket_id)
        row = self.recheck(head=head)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE tranche_completion_rechecks SET status='changed' WHERE id=?", (row["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("DELETE FROM tranche_completion_rechecks WHERE id=?", (row["id"],))
        self.assertEqual(self.ledger.tranche_completion("T"), h1)

    def test_legacy_nullable_recheck_hashes_are_backfilled_and_migration_is_idempotent(self):
        database = self.root / "legacy.db"
        connection = sqlite3.connect(database)
        connection.execute("""
            CREATE TABLE tranche_completion_rechecks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tranche_id TEXT NOT NULL, generation INTEGER NOT NULL,
                previous_generation INTEGER NOT NULL, previous_evidence_hash TEXT NOT NULL,
                correction_plan_ids_json TEXT NOT NULL, accepted_ticket_ids_json TEXT NOT NULL,
                accepted_commit_shas_json TEXT NOT NULL, current_integration_sha TEXT NOT NULL,
                repository_identity TEXT NOT NULL, repo_base_sha TEXT NOT NULL, repo_snapshot_hash TEXT NOT NULL,
                unresolved_correction_count INTEGER NOT NULL, status TEXT NOT NULL, evidence_hash TEXT,
                idempotency_key TEXT NOT NULL UNIQUE, recorded_at INTEGER NOT NULL,
                UNIQUE(tranche_id, generation)
            )
        """)
        values = ("T", 1, 0, "h1", '["P1"]', '["T-F1"]', '["c1"]', "head1", "/repo", "base", "snapshot", 0, "recheck_passed", None, "legacy-key", 123)
        connection.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        connection.commit(); connection.close()

        from local_first_orchestrator.ledger import Ledger
        migrated = Ledger(database)
        migrated.migrate()
        row = migrated.connection.execute("SELECT * FROM tranche_completion_rechecks").fetchone()
        payload = {
            "tranche_id": "T", "generation": 1, "previous_generation": 0,
            "previous_evidence_hash": "h1", "correction_plan_ids": ["P1"],
            "accepted_ticket_ids": ["T-F1"], "accepted_commit_shas": ["c1"],
            "current_integration_sha": "head1", "repository_identity": "/repo",
            "repo_base_sha": "base", "repo_snapshot_hash": "snapshot",
            "unresolved_correction_count": 0, "status": "recheck_passed",
        }
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(row["evidence_hash"], expected)
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.connection.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("T", 2, 1, "h", "[]", "[]", "[]", "head", "/repo", "base", "snapshot", 0, "recheck_passed", None, "bad", 124))
        migrated.migrate()
        self.assertEqual(migrated.connection.execute("SELECT COUNT(*) FROM tranche_completion_rechecks").fetchone()[0], 1)
        migrated.close()
        reopened = Ledger(database); reopened.migrate()
        self.assertEqual(reopened.connection.execute("SELECT evidence_hash FROM tranche_completion_rechecks").fetchone()[0], expected)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
