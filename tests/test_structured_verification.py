from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from local_first_orchestrator.ticket import MicroTicket, PatchBudget, VerificationProfile
from local_first_orchestrator.validation import DeterministicValidator


class StructuredVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        subprocess.run(("git","init","-b","main"),cwd=self.root,check=True,capture_output=True)
        subprocess.run(("git","config","user.email","t@x"),cwd=self.root,check=True); subprocess.run(("git","config","user.name","t"),cwd=self.root,check=True)
        (self.root/"app.py").write_text("def value(): return 'ok'\n"); subprocess.run(("git","add","."),cwd=self.root,check=True); subprocess.run(("git","commit","-m","base"),cwd=self.root,check=True,capture_output=True)
        self.base=subprocess.run(("git","rev-parse","HEAD"),cwd=self.root,text=True,capture_output=True,check=True).stdout.strip()
    def tearDown(self): self.temp.cleanup()
    def validate(self, commands, *, timeout=1, limit=8):
        ticket=MicroTicket("t","Use fixture result.",("C",),"app.py::value",("app.py",),("no api",),PatchBudget(1,10),VerificationProfile(tuple(commands),timeout_seconds=timeout,output_limit=limit),"low",True,2,())
        return DeterministicValidator(artifact_root=self.root/"artifacts").validate(self.root,ticket,base_sha=self.base)
    def test_success_preserves_bounded_evidence(self):
        result=self.validate((("python","-c","import sys; print('stdout-ok'); print('stderr-ok',file=sys.stderr)"),),limit=6)
        self.assertTrue(result.passed); item=result.commands[0]; self.assertEqual(item.returncode,0); self.assertEqual(item.argv[0],"python"); self.assertEqual(item.stdout_summary,"stdout"); self.assertEqual(item.stderr_summary,"stderr"); self.assertTrue(item.truncated)
    def test_nonzero_preserves_real_return_code(self):
        result=self.validate((("python","-c","import sys; print('bad',file=sys.stderr); sys.exit(7)"),))
        self.assertFalse(result.passed); self.assertEqual(result.commands[0].returncode,7)
    def test_timeout_is_structured_and_process_is_reaped(self):
        result=self.validate((("python","-c","while True: pass"),),timeout=1)
        self.assertFalse(result.passed); self.assertEqual(result.commands[0].returncode,DeterministicValidator.TIMEOUT_RETURN_CODE); self.assertTrue(result.commands[0].truncated); self.assertEqual(result.commands[0].stderr_summary,"verifica")
    def test_timeout_reaps_parent_and_descendant_process_group(self):
        pids=self.root/"pids.txt"
        helper=("import os,subprocess,sys,time; p=sys.argv[1]; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open(p,'w').write(f'{os.getpid()} {child.pid}'); time.sleep(30)")
        result=self.validate(((sys.executable,"-c",helper,str(pids)),),timeout=1,limit=128)
        self.assertEqual(result.commands[0].returncode,DeterministicValidator.TIMEOUT_RETURN_CODE)
        parent,child=(int(value) for value in pids.read_text().split())
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            try: os.kill(parent,0); os.kill(child,0)
            except ProcessLookupError: break
            time.sleep(.02)
        for pid in (parent,child):
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
    def test_missing_executable_is_structured(self):
        result=self.validate((("missing-command",),))
        self.assertFalse(result.passed); self.assertEqual(result.commands[0].returncode,DeterministicValidator.LAUNCH_FAILURE_RETURN_CODE)
    def test_non_allowlisted_and_model_proposed_commands_never_spawn(self):
        validator=DeterministicValidator(artifact_root=self.root/"artifacts")
        with patch("local_first_orchestrator.validation.subprocess.run") as run:
            item=validator.run_verification_command(("dangerous-model-command",),allowed_commands=(("python","-c","print(1)"),),cwd=self.root,timeout_seconds=1,output_limit=8)
        self.assertEqual(item.returncode,DeterministicValidator.NOT_ALLOWLISTED_RETURN_CODE); self.assertFalse(run.called)

if __name__=='__main__': unittest.main()
