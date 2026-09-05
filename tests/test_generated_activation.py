import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from local_first_orchestrator.generated_activation import GeneratedActivationContext,activate_generated_ticket
from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.ledger import Ledger
class ActivationTests(unittest.TestCase):
 def setUp(self): self.t=TemporaryDirectory();self.l=Ledger(Path(self.t.name)/'x.db');self.l.migrate();self.ticket=self.l.create_ticket(title='x');self.cfg=RuntimeConfig(Path(self.t.name),Path(self.t.name)/'w',Path(self.t.name)/'a',(Path(self.t.name),));self.ctx=GeneratedActivationContext(self.ticket,'f','tr','p','ext',str(Path(self.t.name)),Path(self.t.name),'a'*40,'h')
 def tearDown(self):self.l.close();self.t.cleanup()
 def test_conflicting_binding_is_not_overwritten(self):
  self.l.bind_runtime(self.ticket,'/other','b'*40)
  with patch('local_first_orchestrator.generated_activation.resolve_generated_activation_context',return_value=self.ctx):r=activate_generated_ticket(self.ticket,self.cfg,self.l)
  self.assertEqual(r.status,'binding_conflict');self.assertEqual(self.l.runtime_binding(self.ticket)['repository_path'],'/other')
 def test_normal_generated_activation_remains_on_normal_path(self):
  with patch('local_first_orchestrator.generated_activation.resolve_generated_activation_context',return_value=self.ctx): r=activate_generated_ticket(self.ticket,self.cfg,self.l)
  self.assertEqual(r.status,'readiness_failed');self.assertEqual(self.l.runtime_binding(self.ticket)['starting_sha'],'a'*40)
