from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.repository_snapshot import snapshot
from local_first_orchestrator.scheduler import ProcessNextScheduler, preview_next
from local_first_orchestrator.states import CanonicalState


class Board:
    timeout_seconds = 2

    def claim_state_projection(self, *args, **kwargs):
        return None

    def claim_evidence_comment(self, *args, **kwargs):
        return None

    def claim_generated_projection(self, *args, **kwargs):
        return None


class SchedulerTrancheCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-qm", "base")
        self.base = self.rev("HEAD")
        self.snap = snapshot(self.repo, self.base)

        self.ledger = Ledger(self.root / "ledger.db")
        self.ledger.migrate()
        self.ledger.connection.execute(
            "INSERT INTO features(id,title,objective,status,created_at,updated_at) VALUES ('F','Feature','Objective','active',1,1)"
        )
        self.ledger.connection.execute(
            "INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?,?)",
            (self.base, json.dumps([["python", "-c", "print('integration-ok')"]])),
        )
        self.ledger.connection.execute(
            "INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at,activated_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES ('P','F','fp','{}','active',1,1,?,?,?,?)",
            (str(self.repo), self.base, self.snap.snapshot_hash, self.snap.manifest_json),
        )
        contract = {
            "objective": "finish tranche",
            "criterion_ids": ["AC"],
            "primary_symbol": "app.py::VALUE",
            "allowed_files": ["app.py"],
            "forbidden_changes": ["none"],
            "patch_budget": {"max_files": 1, "max_changed_lines": 10},
            "verification": {"commands": [["python", "-c", "pass"]]},
            "risk": "low",
            "review_required": True,
            "max_attempts": 1,
            "dependencies": [],
        }
        self.ticket = self.ledger.create_ticket(title="ticket", state=CanonicalState.DONE, contract=contract)
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (self.ticket,))
        self.worktree = self.root / "worktree"
        self.git("worktree", "add", "-q", "-b", "lf/T/1", str(self.worktree), self.base)
        (self.worktree / "app.py").write_text("VALUE = 'done'\n", encoding="utf-8")
        self.wgit("add", "app.py")
        self.wgit("commit", "-qm", "accepted")
        self.commit = self.wrev("HEAD")
        self.git("update-ref", "refs/local-first/tranches/T/integration-head", self.commit)
        self.ledger.connection.execute(
            "INSERT INTO attempts(ticket_id,attempt_number,base_sha,branch,worktree_path,accepted_commit_sha,created_at) VALUES (?,1,?,?,?,?,1)",
            (self.ticket, self.base, "lf/T/1", str(self.worktree), self.commit),
        )
        self.ledger.record_accepted_evidence(self.ticket, self.commit, "diff", "validated")
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", (self.repo,))
        self.controller = LocalFirstController(self.ledger, Board(), self.config)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def git(self, *args: str):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def wgit(self, *args: str):
        return subprocess.run(("git", *args), cwd=self.worktree, text=True, capture_output=True, check=True)

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def wrev(self, ref: str) -> str:
        return self.wgit("rev-parse", ref).stdout.strip()

    def scheduler(self, *, now: int = 100, runner=None) -> ProcessNextScheduler:
        if runner is None:
            runner = lambda tranche_id: self.controller.execute_tranche_checkpoint_only(tranche_id, repository=self.repo)
        return ProcessNextScheduler(
            self.ledger,
            Board(),
            worker_id="checkpoint",
            lease_seconds=30,
            clock=lambda: now,
            tranche_checkpoint_runner=runner,
        )

    def test_checkpoint_persists_completion_and_artifact_without_activation(self) -> None:
        preview = preview_next(self.ledger, now=100)
        self.assertEqual((preview.next_stage, preview.ticket_id), ("tranche_checkpoint", self.ticket))
        result = self.scheduler().process_next()
        self.assertEqual((result.stage, result.status), ("tranche_checkpoint", "completed"))
        completion = self.ledger.tranche_completion("T")
        checkpoint = self.ledger.tranche_checkpoint("T")
        self.assertEqual(completion["final_integration_sha"], self.commit)
        self.assertEqual(checkpoint["completion_evidence_hash"], completion["evidence_hash"])
        self.assertEqual(checkpoint["decision"], "ready_for_checkpoint")
        self.assertTrue(Path(checkpoint["checkpoint_artifact"]).is_file())
        self.assertEqual(self.ledger.connection.execute("SELECT status FROM tranches WHERE id='T'").fetchone()[0], "active")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM tickets WHERE tranche_id='T'").fetchone()[0], 1)

    def test_checkpoint_fails_closed_on_planning_snapshot_corruption(self) -> None:
        self.ledger.connection.execute("UPDATE decomposition_plans SET repo_snapshot_hash=? WHERE id='P'", ("0" * 64,))
        with self.assertRaisesRegex(RuntimeError, "planning snapshot hash drift"):
            self.scheduler().process_next()
        self.assertIsNone(self.ledger.tranche_completion("T"))
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

    def test_integration_failure_is_durable_checkpoint_decision(self) -> None:
        self.ledger.connection.execute(
            "UPDATE tranches SET integration_commands_json=? WHERE id='T'",
            (json.dumps([["python", "-c", "import sys; sys.exit(7)"]]),),
        )
        result = self.scheduler().process_next()
        self.assertEqual(result.stage, "tranche_checkpoint")
        checkpoint = self.ledger.tranche_checkpoint("T")
        self.assertEqual(checkpoint["decision"], "integration_failed")
        integration = json.loads(checkpoint["integration_results_json"])
        self.assertEqual(integration[0]["returncode"], 7)
        self.assertIsNotNone(self.ledger.tranche_completion("T"))

    def test_completed_effect_restarts_without_rerunning_integration(self) -> None:
        claim = self.ledger.claim_next_scheduler_tranche_checkpoint("crashed", lease_seconds=1, now=101)
        self.assertIsNotNone(claim)
        claim_id = str(claim["claim_id"])
        self.ledger.begin_scheduler_claim_effect(claim_id, "crashed", now=101)
        result = self.controller.execute_tranche_checkpoint_only("T", repository=self.repo)
        applied = self.ledger.apply_scheduler_tranche_checkpoint_effect(claim_id, "crashed", result, now=101)
        self.assertIsNotNone(applied["side_effect_completed_at"])
        self.assertIsNone(applied["finalized_at"])
        calls: list[str] = []
        resumed = self.scheduler(
            now=103,
            runner=lambda tranche_id: calls.append(tranche_id) or (_ for _ in ()).throw(AssertionError("integration must not rerun")),
        )
        final = resumed.process_next()
        self.assertEqual((final.stage, final.status), ("tranche_checkpoint", "completed"))
        self.assertEqual(calls, [])
        self.assertEqual(self.ledger.scheduler_claim(claim_id)["status"], "completed")
        self.assertIsNotNone(self.ledger.tranche_checkpoint("T"))

    def test_artifact_written_before_ledger_apply_replays_without_rerunning_commands(self) -> None:
        counter = self.root / "integration-count.txt"
        script = (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "p.write_text(p.read_text()+'x' if p.exists() else 'x')"
        )
        self.ledger.connection.execute(
            "UPDATE tranches SET integration_commands_json=? WHERE id='T'",
            (json.dumps([["python", "-c", script]]),),
        )

        def crash_after_artifact(tranche_id: str):
            self.controller.execute_tranche_checkpoint_only(tranche_id, repository=self.repo)
            raise RuntimeError("simulated death after checkpoint artifact")

        first = self.scheduler(now=100, runner=crash_after_artifact)
        with self.assertRaisesRegex(RuntimeError, "simulated death after checkpoint artifact"):
            first.process_next()
        self.assertEqual(counter.read_text(), "x")
        self.assertTrue((self.root / "artifacts" / "tranches" / "T" / "checkpoint.json").is_file())
        self.assertIsNone(self.ledger.tranche_checkpoint("T"))

        resumed = self.scheduler(now=131)
        result = resumed.process_next()
        self.assertEqual((result.stage, result.status), ("tranche_checkpoint", "completed"))
        self.assertEqual(counter.read_text(), "x")
        self.assertIsNotNone(self.ledger.tranche_checkpoint("T"))


if __name__ == "__main__":
    unittest.main()
