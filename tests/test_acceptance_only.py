from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger, canonical_sha256
from local_first_orchestrator.states import CanonicalState


class Board:
    is_fake = False
    def set_state(self, *args, **kwargs):
        return None


class Model:
    provider = "fixture-provider"
    model = "fixture-model"
    def __init__(self):
        self.calls = []
    def invoke(self, purpose, packet, *, artifact_dir, workdir=None):
        self.calls.append(purpose)
        if purpose == "implementation":
            (Path(workdir) / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")
        artifact = Path(artifact_dir) / f"{purpose}.json"
        artifact.write_text("{}", encoding="utf-8")
        return type("Result", (), {"payload": {}})()


CONTRACT = {
    "objective": "Return the fixture value.", "criterion_ids": ["AC-1"],
    "primary_symbol": "app.py::value", "allowed_files": ["app.py"], "new_test_files": [],
    "forbidden_changes": ["No unrelated change."], "patch_budget": {"max_files": 1, "max_changed_lines": 10},
    "verification": {"commands": [["true"]]}, "risk": "low", "review_required": True, "max_attempts": 2,
}


class AcceptanceOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.root = Path(self.temp.name); self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@example.invalid"); self.git("config", "user.name", "T")
        (self.repo / "app.py").write_text("def value():\n    return 'bad'\n", encoding="utf-8"); self.git("add", "."); self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.ledger = Ledger(self.root / "ledger.db"); self.ledger.migrate(); self.model = Model()
        self.config = RuntimeConfig(self.repo, self.root / "worktrees", self.root / "artifacts", repository_allowlist=(self.repo,))
        body = "<!-- local-first-orchestrator -->\n```local-first-contract\n" + json.dumps(CONTRACT) + "\n```"
        self.controller = LocalFirstController(self.ledger, Board(), self.config, local_model=self.model)
        self.ticket = self.controller.import_card(ExternalTicket("accept-fixture", "accept fixture", body, "scheduled", str(self.repo)))
        self.ledger.pause("operator", reason="acceptance fixture")
        self.prepare_applied_review()

    def tearDown(self):
        self.ledger.close(); self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(("git", *args), cwd=self.repo, text=True, capture_output=True, check=True)

    def prepare_applied_review(self):
        self.controller.execute_implementation(self.ticket, repository=self.repo)
        attempt = self.ledger.connection.execute("select * from attempts where ticket_id=? and attempt_number=1", (self.ticket,)).fetchone()
        candidate = self.ledger.review_candidate(self.ticket, 1); assert attempt and candidate
        artifact = self.root / "review.json"
        artifact.write_text(json.dumps({"payload": {"verdict": "pass", "criterion_results": [{"criterion_id": "AC-1", "status": "pass", "evidence": "fixture"}], "findings": [], "suggestions": []}}), encoding="utf-8")
        invocation = "review-fixture"
        self.ledger.start_model_invocation(invocation_id=invocation, ticket_id=self.ticket, attempt_number=1, stage="review", provider="fixture", model="fixture", packet_hash="a" * 64, worktree_path=str(attempt["worktree_path"]), timeout_seconds=30)
        self.ledger.finish_model_invocation(invocation, status="completed", duration_seconds=0, model_artifact=str(artifact))
        self.ledger.record_model_stage(self.ticket, 1, "review", purpose="review", adapter="fixture", request_hash="a" * 64, response_artifact=str(artifact), worktree_path=str(attempt["worktree_path"]), base_sha=self.base, diff_hash=candidate["candidate_fingerprint"])
        self.ledger.apply_persisted_review(self.ticket, 1)

    def test_accepts_exact_candidate_without_integration(self):
        result = self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"], CanonicalState.DONE.value)
        commit = self.ledger.accepted_commit(self.ticket); self.assertIsNotNone(commit)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base)  # canonical checkout remains separate
        attempt = self.ledger.connection.execute("select * from attempts where ticket_id=? and attempt_number=1", (self.ticket,)).fetchone()
        worktree = Path(attempt["worktree_path"]); self.assertEqual(self.git_w(worktree, "rev-parse", "HEAD"), commit)
        self.assertEqual(self.git_w(worktree, "rev-parse", "HEAD^"), self.base)
        self.assertEqual(self.git_w(worktree, "diff", "--name-only", self.base, "HEAD"), "app.py")
        self.assertEqual(self.ledger.connection.execute("select count(*) from accepted_evidence where ticket_id=?", (self.ticket,)).fetchone()[0], 1)
        self.assertEqual(self.model.calls, ["implementation"])

    def _convert_implementation_to_hermes_reconciliation(self) -> Path:
        attempt = self.ledger.connection.execute("select * from attempts where ticket_id=? and attempt_number=1", (self.ticket,)).fetchone()
        impl = self.ledger.model_stage(self.ticket, 1, "implementation")
        candidate = self.ledger.review_candidate(self.ticket, 1)
        self.assertIsNotNone(attempt); self.assertIsNotNone(impl); self.assertIsNotNone(candidate)
        payload = {
            "external_task_id": "accept-fixture",
            "hermes_run_id": "hermes-run",
            "attempt_number": 1,
            "workspace_path": str(attempt["worktree_path"]),
            "base_sha": self.base,
            "diff_hash": str(impl["diff_hash"]),
            "run_status": "completed",
            "run_outcome": "success",
        }
        artifact = self.root / "hermes-reconciliation.json"
        artifact.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        snapshot_hash = canonical_sha256(payload)
        self.ledger.connection.execute("update tickets set state='implementing' where id=?", (self.ticket,))
        self.ledger.connection.execute("delete from model_invocations where ticket_id=? and attempt_number=1 and stage='implementation'", (self.ticket,))
        self.ledger.connection.execute("delete from model_stage_artifacts where ticket_id=? and attempt_number=1 and stage='implementation'", (self.ticket,))
        self.ledger.connection.execute("update attempts set post_diff_hash=? where ticket_id=? and attempt_number=1", (str(impl["diff_hash"]), self.ticket))
        self.ledger.record_hermes_execution_reconciliation(
            external_task_id="accept-fixture", hermes_run_id=17, ticket_id=self.ticket, attempt_number=1,
            run_status="completed", run_outcome="success", session_id=None, branch_name=str(attempt["branch"]),
            workspace_path=str(attempt["worktree_path"]), base_sha=self.base, head_sha=self.base, diff_hash=str(impl["diff_hash"]),
            artifact_path=str(artifact), snapshot_hash=snapshot_hash,
        )
        self.ledger.connection.execute("update tickets set state='local_review' where id=?", (self.ticket,))
        self.ledger.connection.execute(
            "update model_stage_artifacts set adapter='hermes-dispatch' where ticket_id=? and attempt_number=1 and stage='implementation'",
            (self.ticket,),
        )
        self.ledger.connection.execute(
            "update review_candidates set implementation_invocation_id=? where ticket_id=? and attempt_number=1",
            ("hermes-run:accept-fixture:17", self.ticket),
        )
        return artifact

    def test_accepts_valid_reconciled_hermes_implementation_without_model_invocation(self):
        self._convert_implementation_to_hermes_reconciliation()
        result = self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(result["status"], "accepted")
        evidence = self.ledger.connection.execute("select diff_summary from accepted_evidence where ticket_id=?", (self.ticket,)).fetchone()
        self.assertIn("hermes-run:accept-fixture:17", str(evidence["diff_summary"]))
        self.assertEqual(self.ledger.connection.execute("select count(*) from model_invocations where ticket_id=? and stage='implementation'", (self.ticket,)).fetchone()[0], 0)

    def test_rejects_tampered_reconciled_hermes_artifact(self):
        artifact = self._convert_implementation_to_hermes_reconciliation()
        artifact.write_text(json.dumps({"tampered": True}), encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "artifact integrity"):
            self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)

    def test_replay_is_idempotent_and_does_not_integrate(self):
        first = self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        second = self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(first["accepted_commit_sha"], second["accepted_commit_sha"])
        self.assertEqual(self.ledger.connection.execute("select count(*) from accepted_evidence").fetchone()[0], 1)
        self.assertEqual(self.ledger.connection.execute("select count(*) from attempts where accepted_commit_sha is not null").fetchone()[0], 1)

    def test_crash_after_commit_recovers_exact_head(self):
        original = self.ledger.persist_accepted_candidate
        with mock.patch.object(self.ledger, "persist_accepted_candidate", side_effect=RuntimeError("crash after git commit")):
            with self.assertRaisesRegex(RuntimeError, "crash after git commit"):
                self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertIsNone(self.ledger.accepted_commit(self.ticket))
        result = self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.ledger.connection.execute("select count(*) from accepted_evidence").fetchone()[0], 1)

    def test_mutated_candidate_is_rejected(self):
        attempt = self.ledger.connection.execute("select worktree_path from attempts where ticket_id=? and attempt_number=1", (self.ticket,)).fetchone()
        Path(attempt["worktree_path"]).joinpath("extra.py").write_text("x=1\n")
        with self.assertRaises(PermissionError): self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertIsNone(self.ledger.accepted_commit(self.ticket))

    def test_non_pass_review_is_rejected(self):
        self.ledger.connection.execute("update review_results set verdict='escalate', payload_json=? where ticket_id=?", (json.dumps({"verdict":"escalate","findings":[]}), self.ticket))
        with self.assertRaises(PermissionError): self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)

    def test_integration_ref_is_not_created_or_advanced(self):
        self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranche_completion_rechecks").fetchone()[0], 0)

    def _prepare_integration_ref(self):
        self.controller.accept_reviewed_candidate_only(self.ticket, 1, repository=self.repo)
        self.ledger.connection.execute("insert into features(id,title,status,created_at,updated_at) values ('fixture-feature','fixture','active',0,0)")
        self.ledger.connection.execute("insert into tranches(id,feature_id,ordinal,status,base_sha) values ('fixture-tranche','fixture-feature',0,'active',?)", (self.base,))
        self.ledger.connection.execute("update tickets set feature_id='fixture-feature', tranche_id='fixture-tranche' where id=?", (self.ticket,))
        self.git("update-ref", "refs/local-first/tranches/fixture-tranche/integration-head", self.base)

    def test_integration_only_fast_forwards_exact_accepted_commit(self):
        self._prepare_integration_ref()
        result = self.controller.integrate_accepted_candidate_only(self.ticket, 1, repository=self.repo)
        accepted = self.ledger.accepted_commit(self.ticket)
        self.assertEqual(result["status"], "integrated")
        self.assertEqual(self.git("rev-parse", "refs/local-first/tranches/fixture-tranche/integration-head").stdout.strip(), accepted)
        self.assertEqual(self.ledger.connection.execute("select count(*) from tranche_completion_rechecks").fetchone()[0], 0)

    def test_integration_replay_is_exact_and_idempotent(self):
        self._prepare_integration_ref()
        first = self.controller.integrate_accepted_candidate_only(self.ticket, 1, repository=self.repo)
        second = self.controller.integrate_accepted_candidate_only(self.ticket, 1, repository=self.repo)
        self.assertEqual(first["integration_head"], second["integration_head"])
        self.assertEqual(second["status"], "already_integrated")

    def test_integration_rejects_unexpected_head(self):
        self._prepare_integration_ref()
        other = self.repo / "other.txt"; other.write_text("other\n", encoding="utf-8"); self.git("add", "."); self.git("commit", "-qm", "other")
        self.git("update-ref", "refs/local-first/tranches/fixture-tranche/integration-head", self.git("rev-parse", "HEAD").stdout.strip())
        with self.assertRaises(RuntimeError): self.controller.integrate_accepted_candidate_only(self.ticket, 1, repository=self.repo)

    def test_integration_rejects_not_done_ticket(self):
        self._prepare_integration_ref()
        self.ledger.connection.execute("update tickets set state='local_review' where id=?", (self.ticket,))
        with self.assertRaises(PermissionError): self.controller.integrate_accepted_candidate_only(self.ticket, 1, repository=self.repo)

    @staticmethod
    def git_w(path, *args):
        return subprocess.run(("git", *args), cwd=path, text=True, capture_output=True, check=True).stdout.strip()


if __name__ == "__main__": unittest.main()
