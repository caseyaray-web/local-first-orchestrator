from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.scheduler import ProcessNextScheduler
from local_first_orchestrator.states import CanonicalState


class Board:
    timeout_seconds = 1

    def claim_generated_projection(self, *args, **kwargs): return None
    def claim_state_projection(self, *args, **kwargs): return None
    def claim_evidence_comment(self, *args, **kwargs): return None


class TrancheLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "base")
        self.base = self.rev("HEAD")

        self.git("checkout", "-qb", "integration")
        (self.repo / "app.txt").write_text("base\nintegration\n", encoding="utf-8")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "integration")
        self.final = self.rev("HEAD")
        self.git("checkout", "-q", "main")

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ledger.connection.execute("INSERT INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F','Feature','Objective','active',1,1)")
        self.ledger.connection.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?, '[]')", (self.base,))
        self.ticket = self.ledger.create_ticket(title="landing", state=CanonicalState.DONE)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (self.ticket,))
        ticket_ids_json = json.dumps([self.ticket])
        commit_shas_json = json.dumps([self.final])
        completion_hash = self.ledger.connection.execute(
            "SELECT canonical_completion_hash('T',?,?,?,?)",
            (self.base, self.final, ticket_ids_json, commit_shas_json),
        ).fetchone()[0]
        self.ledger.connection.execute(
            "INSERT INTO tranche_completion_evidence(tranche_id,root_planning_sha,final_integration_sha,accepted_ticket_ids_json,accepted_commit_shas_json,evidence_hash,completed_at) VALUES ('T',?,?,?,?,?,1)",
            (self.base, self.final, ticket_ids_json, commit_shas_json, completion_hash),
        )
        artifact = self.root / "checkpoint.json"
        artifact.write_text("{}", encoding="utf-8")
        self.ledger.connection.execute(
            "INSERT INTO tranche_checkpoint_evidence(tranche_id,feature_id,completion_evidence_hash,final_integration_sha,repository_identity,planning_base_sha,planning_snapshot_hash,integration_commands_json,integration_results_json,checkpoint_artifact,checkpoint_artifact_sha256,decision,created_at) VALUES ('T','F',?,?,?,?,?,'[]','[]',?,?,'ready_for_checkpoint',1)",
            (completion_hash, self.final, str(self.repo), self.base, "snapshot-hash", str(artifact), "checkpoint-sha"),
        )
        self.ledger.connection.execute(
            "INSERT INTO paid_checkpoint_evidence(tranche_id,feature_id,checkpoint_artifact_sha256,checkpoint_completion_hash,scheduler_claim_id,request_key,purpose,provider,model,profile,reservation_id,model_call_id,response_json,decision,rationale,created_at) VALUES ('T','F','checkpoint-sha',?,'paid-claim','paid-request','integration_checkpoint','p','m','profile','reservation','call','{}','approve','ok',1)",
            (completion_hash,),
        )
        self.controller = LocalFirstController(
            self.ledger,
            Board(),
            RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,)),
        )
        self.base_identity = self.ledger.next_scheduler_tranche_landing_identity()
        assert self.base_identity is not None
        self.context = self.controller.inspect_tranche_landing_context(self.base_identity, repository=self.repo)
        self.identity = {**self.base_identity, "canonical_branch": self.context["canonical_branch"], "pre_landing_sha": self.context["pre_landing_sha"]}

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def git(self, *args: str, check: bool = True):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=check)

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def test_successful_no_ff_landing_has_exact_two_parent_provenance(self) -> None:
        result = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        commit = result["landing_commit_sha"]
        self.assertEqual(self.git("show", "-s", "--format=%P", commit).stdout.strip().split(), [self.base, self.final])
        self.assertEqual(self.git("show", "-s", "--format=%B", commit).stdout.strip(), "local-first: complete T")
        self.assertEqual(self.rev("HEAD"), commit)

    def test_dirty_checkout_fails_closed(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "canonical repository is dirty"):
            self.controller.execute_tranche_landing(self.identity, repository=self.repo)

    def test_branch_drift_fails_closed(self) -> None:
        self.git("checkout", "-q", "-b", "other")
        with self.assertRaisesRegex(RuntimeError, "canonical branch drift"):
            self.controller.execute_tranche_landing(self.identity, repository=self.repo)

    def test_head_drift_fails_closed(self) -> None:
        self.git("commit", "--allow-empty", "-qm", "drift")
        with self.assertRaisesRegex(RuntimeError, "canonical head drift"):
            self.controller.execute_tranche_landing(self.identity, repository=self.repo)

    def test_non_ancestor_integration_fails_closed(self) -> None:
        base_tree = self.git("rev-parse", f"{self.base}^{{tree}}").stdout.strip()
        proc = subprocess.run(("git", "commit-tree", base_tree), cwd=self.repo, text=True, input="unrelated\n", capture_output=True, check=True)
        unrelated = proc.stdout.strip()
        identity = {**self.identity, "final_integration_sha": unrelated}
        with self.assertRaisesRegex(RuntimeError, "canonical head is not an ancestor"):
            self.controller.execute_tranche_landing(identity, repository=self.repo)

    def test_failed_merge_operation_aborts_cleanly(self) -> None:
        lock = self.repo / ".git" / "index.lock"
        lock.write_text("locked\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "merge failed; canonical repository restored"):
                self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        finally:
            lock.unlink(missing_ok=True)
        self.assertEqual(self.rev("HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all").stdout.strip(), "")
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_exact_crash_replay_reuses_same_commit(self) -> None:
        first = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        commit = first["landing_commit_sha"]
        replay = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        self.assertEqual(replay["landing_commit_sha"], commit)
        self.assertEqual(self.rev("HEAD"), commit)

    def test_incorrect_replay_commit_with_same_parents_and_message_is_rejected(self) -> None:
        first = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        correct = first["landing_commit_sha"]
        wrong_tree = self.git("rev-parse", f"{self.base}^{{tree}}").stdout.strip()
        # commit-tree needs stdin; use a shell-free subprocess to keep the test explicit.
        proc = subprocess.run(("git", "commit-tree", wrong_tree, "-p", self.base, "-p", self.final), cwd=self.repo, text=True, input="local-first: complete T\n", capture_output=True, check=True)
        fake_sha = proc.stdout.strip()
        self.assertNotEqual(fake_sha, correct)
        self.git("reset", "--hard", "-q", fake_sha)
        with self.assertRaisesRegex(RuntimeError, "canonical head drift"):
            self.controller.execute_tranche_landing(self.identity, repository=self.repo)

    def test_lookalike_replay_commit_with_same_tree_parents_and_message_is_rejected(self) -> None:
        first = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        correct = str(first["landing_commit_sha"])
        correct_tree = self.git("show", "-s", "--format=%T", correct).stdout.strip()
        env = {**os.environ, "GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "other@example.invalid", "GIT_COMMITTER_NAME": "Other", "GIT_COMMITTER_EMAIL": "other@example.invalid"}
        proc = subprocess.run(
            ("git", "commit-tree", correct_tree, "-p", self.base, "-p", self.final),
            cwd=self.repo,
            env=env,
            text=True,
            input="local-first: complete T\n",
            capture_output=True,
            check=True,
        )
        lookalike = proc.stdout.strip()
        self.assertNotEqual(lookalike, correct)
        self.git("reset", "--hard", "-q", lookalike)
        with self.assertRaisesRegex(RuntimeError, "canonical head drift"):
            self.controller.execute_tranche_landing(self.identity, repository=self.repo)

    def test_scheduler_freezes_inspected_context_and_persists_immutable_evidence(self) -> None:
        inspected: list[dict[str, object]] = []
        landed: list[dict[str, object]] = []

        def inspect(identity: dict[str, object]) -> dict[str, object]:
            inspected.append(dict(identity))
            return self.controller.inspect_tranche_landing_context(identity, repository=self.repo)

        def land(identity: dict[str, object]) -> dict[str, object]:
            landed.append(dict(identity))
            return self.controller.execute_tranche_landing(identity, repository=self.repo)

        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="landing",
            lease_seconds=30,
            clock=lambda: 100,
            tranche_landing_context_runner=inspect,
            tranche_landing_runner=land,
        )
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.status), ("tranche_landing", "completed"))
        self.assertEqual(len(inspected), 1)
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["canonical_branch"], "main")
        self.assertEqual(landed[0]["pre_landing_sha"], self.base)
        evidence = self.ledger.tranche_landing("T")
        assert evidence is not None
        self.assertEqual(evidence["canonical_branch"], "main")
        self.assertEqual(evidence["pre_landing_sha"], self.base)
        self.assertEqual(evidence["landing_commit_sha"], self.rev("HEAD"))
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM tranches WHERE id='T'").fetchone()[0], "completed")
        with self.assertRaisesRegex(Exception, "tranche landing evidence is immutable"):
            self.ledger.connection.execute("UPDATE tranche_landing_evidence SET canonical_branch='other' WHERE tranche_id='T'")


    def test_active_claim_blocks_duplicate_fresh_claim_without_integrity_error(self) -> None:
        first = self.ledger.claim_next_scheduler_tranche_landing("worker-a", lease_seconds=30, landing_context=self.context, now=100)
        self.assertIsNotNone(first)
        second = self.ledger.claim_next_scheduler_tranche_landing("worker-b", lease_seconds=30, landing_context=self.context, now=100)
        self.assertIsNone(second)

    def test_crash_after_landing_effect_before_claim_finalize_replays_without_git(self) -> None:
        claim = self.ledger.claim_next_scheduler_tranche_landing("worker-a", lease_seconds=1, landing_context=self.context, now=100)
        assert claim is not None
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "worker-a", now=100)
        landing_result = self.controller.execute_tranche_landing(self.identity, repository=self.repo)
        applied = self.ledger.apply_scheduler_tranche_landing_effect(claim_id, "worker-a", landing_result, now=100)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        calls: list[dict[str, object]] = []

        def should_not_land(identity: dict[str, object]) -> dict[str, object]:
            calls.append(identity)
            raise AssertionError("persisted landing effect must finalize without rerunning Git")

        scheduler = ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="restart",
            lease_seconds=30,
            clock=lambda: 102,
            tranche_landing_context_runner=lambda identity: self.controller.inspect_tranche_landing_context(identity, repository=self.repo),
            tranche_landing_runner=should_not_land,
        )
        result = scheduler.process_next()
        self.assertEqual((result.stage, result.status, result.claim_id), ("tranche_landing", "completed", claim_id))
        self.assertEqual(calls, [])
        stored = self.ledger.scheduler_claim(claim_id)
        self.assertEqual(stored["status"], "completed")
        self.assertIsNotNone(stored["finalized_at"])


if __name__ == "__main__":
    unittest.main()
