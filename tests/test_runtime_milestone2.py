from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket, HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger


def contract(**updates):
    value = {"objective":"Return the fixture value.", "criterion_ids":["AC-1"], "primary_symbol":"app.py::value", "allowed_files":["app.py"], "forbidden_changes":["No API change."], "patch_budget":{"max_files":1,"max_changed_lines":10}, "verification":{"commands":[["python","-c","from app import value; assert value() == 'ok'"]]}, "risk":"low", "review_required":True, "max_attempts":2}
    value.update(updates); return value

class FakeBoard:
    is_fake = False
    def __init__(self): self.writes=[]; self.fail=False
    def set_state(self, ticket_id, state, *, idempotency_key):
        if self.fail: raise RuntimeError("board unavailable")
        self.writes.append((ticket_id, state.value, idempotency_key))

class FakeModel:
    def __init__(self, actions): self.actions=list(actions); self.calls=[]
    def invoke(self, purpose, packet, *, artifact_dir, workdir=None):
        self.calls.append((purpose, packet, Path(workdir) if workdir is not None else None))
        if purpose == "implementation":
            action=self.actions.pop(0)
            action(Path(workdir))
        payload = {"verdict":"pass", "criterion_results":[{"criterion_id":"AC-1","status":"pass","evidence":"verified"}], "findings":[], "suggestions":[]} if purpose == "review" else {}
        return type("Result", (), {"payload":payload})()

class Milestone2(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name); self.repo=self.root/"repo"; self.repo.mkdir()
        self.git("init", "-b", "main"); self.git("config","user.email","t@example.invalid"); self.git("config","user.name","T")
        (self.repo/"app.py").write_text("def value():\n    return 'bad'\n"); self.git("add","."); self.git("commit","-m","base")
        self.ledger=Ledger(self.root/"ledger.db"); self.ledger.migrate(); self.board=FakeBoard()
        self.config=RuntimeConfig(self.repo, self.root/"worktrees", self.root/"artifacts", repository_allowlist=(self.repo,))
    def tearDown(self): self.ledger.close(); self.temp.cleanup()
    def git(self,*args,cwd=None): return subprocess.run(("git",*args),cwd=cwd or self.repo,text=True,capture_output=True,check=True)
    def card(self, **changes):
        body="<!-- local-first-orchestrator -->\n```local-first-contract\n"+json.dumps(contract(**changes))+"\n```"
        return ExternalTicket("card-1","fixture",body,"scheduled",str(self.repo))
    def controller(self, actions): return LocalFirstController(self.ledger,self.board,self.config,local_model=FakeModel(actions))
    @staticmethod
    def good(path): (path/"app.py").write_text("def value():\n    return 'ok'\n")
    @staticmethod
    def outside(path): (path/"evil.py").write_text("x=1\n")
    def test_execute_single_card_commits_and_projects_non_ready(self):
        ctl=self.controller([self.good]); ticket=ctl.import_card(self.card()); ctl.execute(ticket,repository=self.repo,allow_board_writes=True)
        row=self.ledger.get_ticket(ticket); self.assertEqual(row["state"],"done"); self.assertTrue(self.ledger.accepted_commit(ticket)); self.assertEqual([x[1] for x in self.board.writes],["done"]); self.assertNotIn("ready",[x[1] for x in self.board.writes]); self.assertEqual([x[0] for x in ctl.local_model.calls],["implementation","review"]); self.assertNotEqual(ctl.local_model.calls[0][1],ctl.local_model.calls[1][1])
    def test_validation_failure_repairs_same_ticket_and_out_of_scope_is_recorded(self):
        ctl=self.controller([self.outside,self.good]); ticket=ctl.import_card(self.card()); ctl.execute(ticket,repository=self.repo,allow_board_writes=True)
        self.assertEqual(self.ledger.get_ticket(ticket)["state"],"done"); self.assertEqual([x[0] for x in ctl.local_model.calls],["implementation","implementation","review"]); self.assertIn("changed path outside allowlist", self.ledger.runtime_stage(ticket,"validation-1")["detail"])
    def test_refuses_high_risk_and_repository_mismatch_and_dry_run_is_inert(self):
        ctl=self.controller([self.good]); high=ctl.import_card(self.card(risk="high"));
        with self.assertRaisesRegex(PermissionError,"low-risk"): ctl.execute(high,repository=self.repo,allow_board_writes=True)
        normal=ctl.import_card(ExternalTicket("card-2","fixture",self.card().body,"scheduled",str(self.repo)))
        before=list(ctl.local_model.calls)
        self.assertFalse(ctl.execute(normal,repository=self.repo,allow_board_writes=False))
        self.assertEqual(before,ctl.local_model.calls)
        with self.assertRaisesRegex(ValueError,"mismatch"): ctl.execute(normal,repository=self.root,allow_board_writes=True)
    def test_board_failure_after_commit_does_not_repeat_model_or_commit(self):
        ctl=self.controller([self.good]); ticket=ctl.import_card(self.card()); self.board.fail=True
        with self.assertRaisesRegex(RuntimeError,"board unavailable"): ctl.execute(ticket,repository=self.repo,allow_board_writes=True)
        calls=len(ctl.local_model.calls); commit=self.ledger.accepted_commit(ticket); self.board.fail=False
        ctl.execute(ticket,repository=self.repo,allow_board_writes=True)
        self.assertEqual(calls,len(ctl.local_model.calls)); self.assertEqual(commit,self.ledger.accepted_commit(ticket)); self.assertEqual(len(self.board.writes),1)
    def test_duplicate_claim_is_exclusive(self):
        ctl=self.controller([self.good]); ticket=ctl.import_card(self.card()); self.assertTrue(self.ledger.claim_specific(ticket,"one",60)); self.assertFalse(self.ledger.claim_specific(ticket,"two",60))
    def test_pause_blocks_execute_admission_without_interrupting_existing_work(self):
        ctl=self.controller([self.good]); ticket=ctl.import_card(self.card()); self.ledger.pause("operator", reason="maintenance")
        self.assertFalse(ctl.execute(ticket,repository=self.repo,allow_board_writes=True))
        self.assertEqual(ctl.local_model.calls,[])
        self.assertEqual(self.ledger.get_ticket(ticket)["state"],"ready_local")

if __name__ == "__main__": unittest.main()
