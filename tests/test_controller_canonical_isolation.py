from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger


class Board:
    is_fake = False
    def set_state(self, ticket_id: str, state: object, *, idempotency_key: str) -> None: pass


def contract(new_test_files: list[str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"objective":"Update the fixture value so the declared verification passes.", "criterion_ids":["AC-1"], "primary_symbol":"app.py::value", "allowed_files":["app.py"], "forbidden_changes":["no api"], "patch_budget":{"max_files":2,"max_changed_lines":80}, "verification":{"commands":[["python","-c","from app import value; assert value() == 'ok'"]]}, "risk":"low", "review_required":True, "max_attempts":2}
    if new_test_files: result["new_test_files"] = new_test_files
    return result


class IsolationModel:
    provider = "fixture"; model = "fixture"
    def __init__(self, assertion=None) -> None: self.assertion = assertion
    def invoke(self, purpose: str, packet: str, *, artifact_dir: Path, workdir: Path | None = None) -> object:
        if purpose == "implementation":
            assert workdir is not None
            if self.assertion: self.assertion(workdir)
            (workdir / "app.py").write_text("def value():\n    return 'ok'\n", encoding="utf-8")
            test = workdir / "scripts/test-new-contract.mjs"
            if self.assertion: test.parent.mkdir(exist_ok=True); test.write_text("attempt-owned\n", encoding="utf-8")
        artifact = artifact_dir / f"{purpose}.json"; artifact.write_text("{}", encoding="utf-8")
        payload = {} if purpose == "implementation" else {"verdict":"pass","criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"ok"}],"findings":[],"suggestions":[]}
        return type("Result", (), {"payload": payload, "artifact_path": artifact})()


class ControllerCanonicalIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=self.root/"repo"; self.repo.mkdir()
        self.git("init","-q","-b","main"); self.git("config","user.email","t@example.invalid"); self.git("config","user.name","Test")
        (self.repo/"app.py").write_text("def value():\n    return 'bad'\n", encoding="utf-8"); self.git("add","."); self.git("commit","-qm","base")
        self.ledger=Ledger(self.root/"ledger.db"); self.ledger.migrate(); self.config=RuntimeConfig(self.repo,self.root/"external-worktrees",self.root/"external-artifacts",(self.repo,))
    def tearDown(self) -> None: self.ledger.close(); self.temp.cleanup()
    def git(self,*args: str) -> subprocess.CompletedProcess[str]: return subprocess.run(("git",*args),cwd=self.repo,text=True,capture_output=True,check=True)
    def execute(self, model: IsolationModel, *, new: list[str] | None = None) -> str:
        body="<!-- local-first-orchestrator -->\n```local-first-contract\n"+json.dumps(contract(new))+"\n```"
        ticket=LocalFirstController(self.ledger,Board(),self.config,local_model=model).import_card(ExternalTicket("card","fixture",body,"scheduled",str(self.repo)))
        LocalFirstController(self.ledger,Board(),self.config,local_model=model).execute(ticket,repository=self.repo,allow_board_writes=True)
        return ticket
    def test_preparation_preserves_dirty_canonical_and_never_creates_canonical_controller_roots(self) -> None:
        (self.repo/"user-file").write_text("operator edit\n",encoding="utf-8")
        (self.repo/"user-untracked-file").write_text("operator untracked\n",encoding="utf-8")
        head=self.git("rev-parse","HEAD").stdout; status=self.git("status","--porcelain=v1").stdout
        self.execute(IsolationModel())
        self.assertEqual(self.git("rev-parse","HEAD").stdout,head); self.assertEqual(self.git("status","--porcelain=v1").stdout,status)
        self.assertFalse((self.repo/".hermes/local-first-worktrees").exists()); self.assertFalse((self.repo/".hermes/local-first-artifacts").exists())
    def test_untracked_same_path_is_absent_before_inference_and_remains_canonical_owned(self) -> None:
        canonical = self.repo/"scripts/test-new-contract.mjs"; canonical.parent.mkdir(); canonical.write_text("canonical-owned\n",encoding="utf-8")
        canonical_hash=hashlib.sha256(canonical.read_bytes()).hexdigest()
        def assert_clean_attempt(workdir: Path) -> None: self.assertFalse((workdir/"scripts/test-new-contract.mjs").exists())
        ticket=self.execute(IsolationModel(assert_clean_attempt),new=["scripts/test-new-contract.mjs"])
        attempt=Path(self.ledger.connection.execute("SELECT worktree_path FROM attempts WHERE ticket_id=?",(ticket,)).fetchone()[0])
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(),canonical_hash); self.assertEqual((attempt/"scripts/test-new-contract.mjs").read_text(encoding="utf-8"),"attempt-owned\n")

if __name__ == "__main__": unittest.main()
