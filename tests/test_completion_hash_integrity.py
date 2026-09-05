from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.ledger import Ledger, _completion_evidence_hash, _hash_recheck_payload
from local_first_orchestrator.tranche_completion import completion_evidence
from tests.test_tranche_completion_rechecks import TrancheCompletionRecheckTests


class CompletionHashIntegrityTests(TrancheCompletionRecheckTests):
    def test_h1_producer_and_persistence_validator_share_canonical_hash(self):
        produced = completion_evidence(self.ledger, self.repo, "T")
        self.ledger.record_tranche_completion(produced)
        stored = self.ledger.tranche_completion("T")
        self.assertEqual(produced["evidence_hash"], stored["evidence_hash"])
        self.assertEqual(stored["evidence_hash"], _completion_evidence_hash(stored))
        source = Path(__file__).parents[1] / "local_first_orchestrator" / "tranche_completion.py"
        self.assertNotIn("import hashlib", source.read_text())
        self.assertIn("canonical_sha256", source.read_text())

    def test_h1_api_derives_hash_instead_of_trusting_caller(self):
        supplied = self.record_h1()
        self.assertNotEqual(supplied["evidence_hash"], "h1-evidence")
        self.assertEqual(supplied["evidence_hash"], _completion_evidence_hash(supplied))

    def test_direct_sql_forged_h1_hash_is_rejected(self):
        self.record_h1()
        now = self.ledger._now()
        self.ledger.connection.execute("INSERT INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F2','f2','o','planned',?,?)", (now, now))
        self.ledger.connection.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T2','F2',0,'active',?,'[]')", (self.base,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("INSERT INTO tranche_completion_evidence(tranche_id,root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json,evidence_hash,completed_at) VALUES (?,?,?,?,?,?,?)", ("T2", self.base, self.accepted_map["A"], '["A"]', json.dumps([self.accepted_map["A"]]), "0" * 64, now))

    def test_direct_sql_forged_recheck_hash_is_rejected(self):
        h1 = self.record_h1(); plan = self.service().create_plan(self.spec("hash")); correction = self.service().materialize(plan.correction_plan_id)
        head = self.accept_correction(correction.ticket_id)
        row = self.recheck()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("T", 2, 1, row["evidence_hash"], json.dumps([plan.correction_plan_id]), json.dumps([correction.ticket_id]), json.dumps([head]), head, str(self.repo), self.base, "snapshot", 0, "recheck_passed", "0" * 64, "forged", self.ledger._now()))
        self.assertEqual(self.ledger.tranche_completion("T")["evidence_hash"], h1["evidence_hash"])

    def test_canonical_recheck_hash_succeeds_and_matches_persisted_content(self):
        self.record_h1(); plan = self.service().create_plan(self.spec("canonical")); correction = self.service().materialize(plan.correction_plan_id)
        head = self.accept_correction(correction.ticket_id); row = self.recheck()
        payload = {"tranche_id": row["tranche_id"], "generation": row["generation"], "previous_generation": row["previous_generation"], "previous_evidence_hash": row["previous_evidence_hash"], "correction_plan_ids": json.loads(row["correction_plan_ids_json"]), "accepted_ticket_ids": json.loads(row["accepted_ticket_ids_json"]), "accepted_commit_shas": json.loads(row["accepted_commit_shas_json"]), "current_integration_sha": row["current_integration_sha"], "repository_identity": row["repository_identity"], "repo_base_sha": row["repo_base_sha"], "repo_snapshot_hash": row["repo_snapshot_hash"], "unresolved_correction_count": row["unresolved_correction_count"], "status": row["status"]}
        self.assertEqual(row["evidence_hash"], _hash_recheck_payload(payload))

    def test_legacy_nullable_recheck_hash_is_backfilled_idempotently(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE tranche_completion_rechecks (id INTEGER PRIMARY KEY AUTOINCREMENT,tranche_id TEXT NOT NULL,generation INTEGER NOT NULL,previous_generation INTEGER NOT NULL,previous_evidence_hash TEXT NOT NULL,correction_plan_ids_json TEXT NOT NULL,accepted_ticket_ids_json TEXT NOT NULL,accepted_commit_shas_json TEXT NOT NULL,current_integration_sha TEXT NOT NULL,repository_identity TEXT NOT NULL,repo_base_sha TEXT NOT NULL,repo_snapshot_hash TEXT NOT NULL,unresolved_correction_count INTEGER NOT NULL,status TEXT NOT NULL,evidence_hash TEXT,idempotency_key TEXT NOT NULL UNIQUE,recorded_at INTEGER NOT NULL,UNIQUE(tranche_id,generation))")
            values = ("T", 1, 0, "h1", '["p"]', '["t"]', '["c"]', "head", "repo", "base", "snapshot", 0, "recheck_passed", None, "key", 1)
            raw.execute("INSERT INTO tranche_completion_rechecks(tranche_id,generation,previous_generation,previous_evidence_hash,correction_plan_ids_json,accepted_ticket_ids_json,accepted_commit_shas_json,current_integration_sha,repository_identity,repo_base_sha,repo_snapshot_hash,unresolved_correction_count,status,evidence_hash,idempotency_key,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            raw.commit(); raw.close()
            ledger = Ledger(path); ledger.migrate()
            row = ledger.connection.execute("SELECT * FROM tranche_completion_rechecks").fetchone()
            expected = _hash_recheck_payload({"tranche_id": "T", "generation": 1, "previous_generation": 0, "previous_evidence_hash": "h1", "correction_plan_ids": ["p"], "accepted_ticket_ids": ["t"], "accepted_commit_shas": ["c"], "current_integration_sha": "head", "repository_identity": "repo", "repo_base_sha": "base", "repo_snapshot_hash": "snapshot", "unresolved_correction_count": 0, "status": "recheck_passed"})
            self.assertEqual(row["evidence_hash"], expected)
            ledger.migrate()
            self.assertEqual(ledger.connection.execute("SELECT evidence_hash FROM tranche_completion_rechecks").fetchone()[0], expected)
            ledger.close()


if __name__ == "__main__":
    unittest.main()
