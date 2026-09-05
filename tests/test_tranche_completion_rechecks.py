from __future__ import annotations

import json
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

    def recheck(self, generation, previous_generation, previous_hash, plan_ids, ticket_ids, commits, head, unresolved=0):
        return self.ledger.record_tranche_completion_recheck({
            "tranche_id": "T", "generation": generation, "previous_generation": previous_generation,
            "previous_evidence_hash": previous_hash, "correction_plan_ids": plan_ids,
            "accepted_ticket_ids": ticket_ids, "accepted_commit_shas": commits,
            "current_integration_sha": head, "repository_identity": str(self.repo),
            "repo_base_sha": self.base, "repo_snapshot_hash": "snapshot",
            "unresolved_correction_count": unresolved,
            "status": "recheck_passed" if unresolved == 0 else "open_corrections",
        })

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


if __name__ == "__main__":
    unittest.main()
