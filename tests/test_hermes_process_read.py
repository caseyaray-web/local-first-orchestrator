from __future__ import annotations
import os,stat,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.hermes_board import HermesBoardAdapter
class ProcessReadTests(unittest.TestCase):
 def setUp(self):
  self.t=TemporaryDirectory(); self.log=Path(self.t.name)/'log'; self.exe=Path(self.t.name)/'hermes'; self.exe.write_text('#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ["LOG"],"a").write(" ".join(sys.argv[1:])+"\\n")\na=sys.argv[1:]\nif a[:4]!=["kanban","--board","board","list"] and a[:4]!=["kanban","--board","board","show"]: sys.exit(9)\nif "list" in a: print(json.dumps([{ "id":"1","title":"x","body":"<!-- local-first-orchestrator -->","status":"scheduled"}]))\nelse: print(json.dumps({"task":{"id":"1","title":"x","body":"b","status":"scheduled"}}))\n'); self.exe.chmod(stat.S_IRWXU); self.old=os.environ.get('LOG'); os.environ['LOG']=str(self.log)
 def tearDown(self):
  if self.old is None: os.environ.pop('LOG',None)
  else: os.environ['LOG']=self.old
  self.t.cleanup()
 def test_reads_use_absolute_fake_and_explicit_board(self):
  a=HermesBoardAdapter(executable=str(self.exe),board='board'); self.assertEqual(len(a.import_candidates()),1); self.assertEqual(a.get_task('1').id,'1'); self.assertIn('kanban --board board list --json --status scheduled',self.log.read_text()); self.assertIn('kanban --board board show 1 --json',self.log.read_text())
 def test_missing_or_invalid_board_or_executable_fails_closed(self):
  with self.assertRaises(ValueError): HermesBoardAdapter(executable='hermes',board='')
  with self.assertRaises(ValueError): HermesBoardAdapter(executable='/missing/hermes',board='board')
if __name__=='__main__': unittest.main()
