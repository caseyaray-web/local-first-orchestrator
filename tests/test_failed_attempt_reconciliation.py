from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.git_adapter import GitWorktreeAdapter
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger


def contract() -> dict[str, object]:
    return {"objective":"Update the fixture value so the declared verification passes.","criterion_ids":["AC-1"],"primary_symbol":"app.py::value","allowed_files":["app.py"],"forbidden_changes":["no API change"],"patch_budget":{"max_files":1,"max_changed_lines":20},"verification":{"commands":[["python","-c","from app import value; assert value() == 'ok'"]]},"risk":"low","review_required":True,"max_attempts":1}


class Board:
    is_fake = False
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass


class Model:
    provider = "fixture-provider"; model = "fixture-model"
    def __init__(self, mode: str, assertion=None) -> None: self.mode, self.assertion, self.calls = mode, assertion, []
    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        self.calls.append(purpose)
        if purpose == "implementation":
            assert workdir is not None
            if self.assertion: self.assertion(workdir)
            (workdir / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")
            if self.mode == "timeout": raise subprocess.TimeoutExpired(("fixture",), 1)
        artifact = artifact_dir / f"{purpose}.json"; artifact.write_text("{}", encoding="utf-8")
        payload = {} if purpose == "implementation" else {"verdict":"pass","criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"ok"}],"findings":[],"suggestions":[]}
        return type("Result", (), {"payload":payload,"artifact_path":artifact})()


class FailedAttemptReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=self.root/"repo"; self.repo.mkdir()
        self.git("init","-q","-b","main"); self.git("config","user.email","t@example.invalid"); self.git("config","user.name","Test")
        (self.repo/"app.py").write_text("def value():\n    return 'bad'\n",encoding="utf-8"); self.git("add","."); self.git("commit","-qm","base")
        self.base=self.git("rev-parse","HEAD").stdout.strip(); self.ledger=Ledger(self.root/"ledger.db"); self.ledger.migrate()
        self.config=RuntimeConfig(self.repo,self.root/"external-worktrees",self.root/"external-artifacts",(self.repo,),implementation_timeout_seconds=31)
        card=ExternalTicket("card","fixture","<!-- local-first-orchestrator -->\n```local-first-contract\n"+json.dumps(contract())+"\n```","scheduled",str(self.repo))
        self.ticket=LocalFirstController(self.ledger,Board(),self.config,local_model=Model("good")).import_card(card)
        self.ledger.connection.execute("INSERT INTO features(id,title,status,created_at,updated_at) VALUES ('F','fixture','active',1,1)")
        self.ledger.connection.execute("INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES ('T','F',0,'active',?,'[]')", (self.base,))
        self.ledger.connection.execute("UPDATE tickets SET feature_id='F',tranche_id='T' WHERE id=?", (self.ticket,))
        GitWorktreeAdapter(self.repo,self.config.worktree_root).resolve_execution_base("T",self.base)
    def tearDown(self) -> None: self.ledger.close(); self.temp.cleanup()
    def git(self,*args: str) -> subprocess.CompletedProcess[str]: return subprocess.run(("git",*args),cwd=self.repo,text=True,capture_output=True,check=True)
    def controller(self, model: Model) -> LocalFirstController: return LocalFirstController(self.ledger,Board(),self.config,local_model=model)
    def fail_attempt_one(self) -> tuple[Path, Path]:
        with self.assertRaises(subprocess.TimeoutExpired): self.controller(Model("timeout")).execute(self.ticket,repository=self.repo,allow_board_writes=True)
        row=self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=? AND attempt_number=1",(self.ticket,)).fetchone(); assert row is not None
        return Path(row["worktree_path"]), self.root/"external-artifacts"/self.ticket/"1"
    def reconcile(self, artifact: Path) -> dict[str, object]:
        self.ledger.pause("operator",reason="forensic reconciliation")
        return self.controller(Model("good")).reconcile_failed_attempt(self.ticket,operator_id="operator",classification="runtime_infrastructure_failure",forensic_artifact_paths=(artifact,))
    def integration_head(self) -> str:
        return self.git("show-ref","--verify","--hash","refs/local-first/tranches/T/integration-head").stdout.strip()
    def clean_retired_attempt(self, worktree: Path, artifact: Path) -> None:
        self.git("worktree","remove","--force",str(worktree)); self.git("branch","-D","local-first/"+self.ticket+"/attempt-1"); shutil.rmtree(artifact)
    def test_reconciliation_retires_history_idempotently_without_creating_attempt_two(self) -> None:
        path, artifact=self.fail_attempt_one(); self.assertTrue(path.exists()); self.assertTrue(artifact.exists())
        before=self.integration_head(); result=self.reconcile(artifact); self.assertEqual(result["status"],"reconciled")
        attempt=self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=1",(self.ticket,)).fetchone(); assert attempt is not None
        self.assertEqual(attempt["outcome"],"failed_retired"); self.assertEqual(attempt["base_sha"],self.base); self.assertEqual(Path(attempt["worktree_path"]),path); self.assertIsNone(attempt["failure_fingerprint"])
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"],"ready_local"); self.assertEqual(self.ledger.attempt_count(self.ticket),1); self.assertEqual(self.integration_head(),before)
        record=self.ledger.failed_attempt_reconciliation(self.ticket,1); assert record is not None; self.assertEqual(record["prospective_next_attempt_number"],2); self.assertEqual(record["cleanup_required"],1); self.assertIn(str(artifact),record["forensic_artifact_paths_json"]); self.assertIn('"implementation_timeout_seconds":31',record["runtime_identity_json"])
        self.assertEqual(self.ledger.cleanup_prerequisites()[0]["cleanup_confirmed"],False)
        replay=self.reconcile(artifact); self.assertEqual(replay["status"],"already_reconciled"); self.assertEqual(self.ledger.attempt_count(self.ticket),1); self.assertTrue(path.exists()); self.assertTrue(artifact.exists())
    def test_retry_requires_separate_cleanup_confirmation_then_uses_clean_attempt_two(self) -> None:
        first, artifact=self.fail_attempt_one(); first_contents=(first/"app.py").read_text(encoding="utf-8"); self.assertIn("ok",first_contents)
        head=self.integration_head(); self.reconcile(artifact); self.ledger.resume("operator",reason="authorized test retry")
        with self.assertRaisesRegex(RuntimeError,"cleanup confirmation"):
            self.controller(Model("good")).execute(self.ticket,repository=self.repo,allow_board_writes=True)
        self.assertEqual(self.ledger.get_ticket(self.ticket)["state"],"ready_local")
        self.ledger.pause("operator",reason="separate cleanup confirmation")
        self.clean_retired_attempt(first,artifact)
        confirmed=self.controller(Model("good")).confirm_retired_attempt_cleanup(self.ticket,operator_id="operator"); self.assertEqual(confirmed["status"],"confirmed")
        self.assertEqual(self.ledger.cleanup_prerequisites()[0]["cleanup_confirmed"],True)
        self.ledger.resume("operator",reason="authorized test retry")
        def clean(worktree: Path) -> None:
            self.assertNotEqual(worktree,first); self.assertEqual((worktree/"app.py").read_text(encoding="utf-8"),"def value():\n    return 'bad'\n")
        self.assertTrue(self.controller(Model("good",clean)).execute(self.ticket,repository=self.repo,allow_board_writes=True))
        second=self.ledger.connection.execute("SELECT * FROM attempts WHERE ticket_id=? AND attempt_number=2",(self.ticket,)).fetchone(); assert second is not None
        self.assertEqual(second["base_sha"],head); self.assertEqual(Path(second["worktree_path"]),self.root/"external-worktrees"/self.ticket/"attempt-2"); self.assertNotEqual(Path(second["worktree_path"]),first)
    def test_accepted_attempt_cannot_be_retired(self) -> None:
        _, artifact=self.fail_attempt_one(); self.ledger.record_accepted_evidence(self.ticket,"a"*40,"diff","validation"); self.ledger.pause("operator",reason="test")
        with self.assertRaisesRegex(ValueError,"accepted ticket"): self.controller(Model("good")).reconcile_failed_attempt(self.ticket,operator_id="operator",classification="runtime_infrastructure_failure",forensic_artifact_paths=(artifact,))
    def test_incomplete_invocation_must_be_resolved_before_reconciliation(self) -> None:
        _, artifact=self.fail_attempt_one(); self.ledger.start_model_invocation(invocation_id="incomplete",ticket_id=self.ticket,attempt_number=1,stage="review",provider="fixture",model="fixture",packet_hash="a"*64,worktree_path="/tmp/forensic",timeout_seconds=31); self.ledger.pause("operator",reason="test")
        with self.assertRaisesRegex(ValueError,"incomplete model invocation"): self.controller(Model("good")).reconcile_failed_attempt(self.ticket,operator_id="operator",classification="runtime_infrastructure_failure",forensic_artifact_paths=(artifact,))

if __name__ == "__main__": unittest.main()
