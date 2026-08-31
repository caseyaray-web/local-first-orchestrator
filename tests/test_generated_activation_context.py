import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.generated_activation import GeneratedActivationError,resolve_generated_activation_context
from local_first_orchestrator.controller import RuntimeConfig
from local_first_orchestrator.ledger import Ledger
class ContextTests(unittest.TestCase):
 def test_missing_ticket_is_read_only(self):
  d=TemporaryDirectory();l=Ledger(Path(d.name)/'x.db');l.migrate();cfg=RuntimeConfig(Path(d.name),Path(d.name)/'w',Path(d.name)/'a',(Path(d.name),))
  with self.assertRaisesRegex(GeneratedActivationError,'ticket_not_found'):resolve_generated_activation_context('missing',cfg,l)
  self.assertEqual(l.connection.execute('select count(*) from runtime_bindings').fetchone()[0],0);l.close();d.cleanup()
