import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.cli import main
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.decomposition import PlanValidator, activate_validated_plan
from tests.test_generated_projection_delivery import GeneratedProjectionDeliveryTests

class GeneratedProjectionCliTests(unittest.TestCase):
 def test_project_generated_requires_existing_write_gate_before_claim(self):
  d=TemporaryDirectory(); db=Path(d.name)/'x.db'; l=Ledger(db);l.migrate()
  from tests.test_decomposition import Plans
  plans=Plans();feature=plans.feature();plan=plans.plan();activate_validated_plan(l,feature,plan,PlanValidator().validate(feature,plan),plans.repository_validation(plan)); l.close()
  with self.assertRaisesRegex(PermissionError,'allow-board-writes'):
   main(['--database',str(db),'project-generated'])
  reopened=Ledger(db);reopened.migrate();self.assertIsNone(reopened.connection.execute("select lease_owner from board_projection_outbox where operation='create_microticket' limit 1").fetchone()[0]);reopened.close();d.cleanup()
