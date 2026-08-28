from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local_first_orchestrator.controller import LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.local_qwen import LocalQwenAdapter


class FakeHermes:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows, self.calls, self.fail, self.malformed = rows, [], False, False
    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        if self.fail: return subprocess.CompletedProcess(argv, 2, "", "simulated failure")
        if self.malformed: return subprocess.CompletedProcess(argv, 0, "not-json", "")
        if "show" in argv:
            task_id = argv[argv.index("show") + 1]
            row = next((row for row in self.rows if row["id"] == task_id), None)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"task": row}) if row else json.dumps({}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.rows), "")


def contract() -> dict[str, object]:
    return {"objective":"Change one symbol.","criterion_ids":["AC-1"],"primary_symbol":"app.py::run","allowed_files":["app.py","test_app.py"],"forbidden_changes":["No API change."],"patch_budget":{"max_files":2,"max_changed_lines":20},"verification":{"commands":[["python","-c","print('ok')"]]},"risk":"low","review_required":True,"max_attempts":2}


class RuntimeMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); root=Path(self.tmp.name)
        subprocess.run(("git","init","-b","main"),cwd=root,text=True,capture_output=True,check=True)
        subprocess.run(("git","config","user.email","fixture@example.invalid"),cwd=root,text=True,capture_output=True,check=True)
        subprocess.run(("git","config","user.name","Fixture"),cwd=root,text=True,capture_output=True,check=True)
        (root / "README.md").write_text("fixture\n")
        subprocess.run(("git","add","."),cwd=root,text=True,capture_output=True,check=True)
        subprocess.run(("git","commit","-m","fixture"),cwd=root,text=True,capture_output=True,check=True)
        self.ledger=Ledger(root/"ledger.db"); self.ledger.migrate()
        body="<!-- local-first-orchestrator -->\n```local-first-contract\n"+json.dumps(contract())+"\n```"
        self.fake=FakeHermes([{"id":"t1","title":"eligible","body":body,"status":"scheduled","workspace_path":None}])
        self.board=HermesBoardAdapter(runner=self.fake)
        self.controller=LocalFirstController(self.ledger,self.board,RuntimeConfig(root,root/"wt",root/"art"))
    def tearDown(self): self.ledger.close(); self.tmp.cleanup()
    def test_import_idempotent_and_dry_run_has_no_external_effects(self):
        card=self.board.get_task("t1"); first=self.controller.import_card(card); second=self.controller.import_card(card)
        self.assertEqual(first,second); self.assertEqual(self.ledger.status()["tickets"]["ready_local"],1)
        before=list(self.fake.calls); plan=self.controller.dry_run(first)
        self.assertEqual(before,self.fake.calls); self.assertFalse(plan["would_invoke_model"]); self.assertFalse(plan["would_write_board"]); self.assertFalse(plan["would_modify_repository"])
    def test_rejects_ineligible_contract_and_cli_failures_closed(self):
        bad=type("Card",(),{"id":"bad","title":"bad","body":"<!-- local-first-orchestrator -->","status":"scheduled"})()
        with self.assertRaisesRegex(ValueError,"contract block"): self.controller.import_card(bad)
        incomplete = contract(); del incomplete["objective"]; del incomplete["risk"]
        missing = type("Card",(),{"id":"missing","title":"missing","body":"<!-- local-first-orchestrator -->\n```local-first-contract\n" + json.dumps(incomplete) + "\n```","status":"scheduled"})()
        with self.assertRaisesRegex(ValueError, "objective, risk"): self.controller.import_card(missing)
        self.fake.malformed=True
        with self.assertRaises(json.JSONDecodeError): self.board.import_candidates()
        self.fake.malformed=False; self.fake.fail=True
        with self.assertRaisesRegex(RuntimeError,"simulated failure"): self.board.import_candidates()
    def test_lease_is_exclusive_and_restart_does_not_duplicate_import(self):
        ticket=self.controller.import_card(self.board.get_task("t1"))
        self.assertEqual(self.ledger.claim_ticket("one",lease_seconds=60),ticket); self.assertIsNone(self.ledger.claim_ticket("two",lease_seconds=60))
        self.ledger.close(); self.ledger=Ledger(Path(self.tmp.name)/"ledger.db"); self.ledger.migrate()
        self.controller=LocalFirstController(self.ledger,self.board,RuntimeConfig(Path(self.tmp.name),Path(self.tmp.name)/"wt",Path(self.tmp.name)/"art"))
        self.assertEqual(self.controller.import_card(self.board.get_task("t1")),ticket)
    def test_qwen_boundary_accepts_worktree_cwd_without_invocation_in_dry_run(self):
        calls=[]
        def runner(argv,**kwargs): calls.append(kwargs); return subprocess.CompletedProcess(argv,0,"{}","")
        result=LocalQwenAdapter(runner=runner).invoke("implementation","{}",artifact_dir=Path(self.tmp.name),workdir=Path(self.tmp.name))
        self.assertEqual(result.payload,{}); self.assertEqual(calls[0]["cwd"],self.tmp.name)

if __name__ == "__main__": unittest.main()
