import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState


def contract(dependencies=()):
 return {'objective':'Return fixture value.','criterion_ids':['A'],'primary_symbol':'a.py::f','allowed_files':['a.py'],'forbidden_changes':['No API change.'],'patch_budget':{'max_files':1,'max_changed_lines':10,'exception_reason':None},'verification':{'commands':[['python','-c','print(1)']],'working_directory':'.'},'risk':'low','review_required':True,'max_attempts':2,'dependencies':list(dependencies)}
class ReadinessTests(unittest.TestCase):
 def setUp(self): self.t=TemporaryDirectory();self.l=Ledger(Path(self.t.name)/'x.db');self.l.migrate()
 def tearDown(self): self.l.close();self.t.cleanup()
 def ticket(self,deps=(),state=CanonicalState.DRAFT):
  x=self.l.create_ticket(title='x',state=state,contract=contract(deps));self.l.bind_runtime(x,'/repo','a'*40);return x
 def test_admission_dependencies_and_replay_are_atomic(self):
  b=self.ticket();a=self.ticket((b,));self.assertEqual(self.l.evaluate_ticket_readiness(a).status,'waiting_on_dependencies');self.l.transition(b,CanonicalState.READY_LOCAL);self.l.transition(b,CanonicalState.IMPLEMENTING);self.l.transition(b,CanonicalState.VERIFYING);self.l.transition(b,CanonicalState.ACCEPTED);self.assertEqual(self.l.evaluate_ticket_readiness(a).status,'ready');self.l.admit_ticket_if_ready(a);self.l.admit_ticket_if_ready(a);self.assertEqual(self.l.get_ticket(a)['state'],'ready_local');self.assertEqual(len([e for e in self.l.events_for(a) if e['to_state']=='ready_local']),1);self.assertEqual(self.l.connection.execute("select count(*) from board_projection_outbox where ticket_id=?",(a,)).fetchone()[0],1);self.assertEqual(self.l.attempt_count(a),0)
 def test_missing_multiple_and_self_dependencies_fail_closed(self):
  a=self.ticket(('z','a'));self.l.connection.execute("update tickets set dependencies_json=? where id=?",('["z","'+a+'"]',a));self.assertEqual(self.l.evaluate_ticket_readiness(a).status,'invalid_ticket');b=self.ticket(('z','y'));self.assertEqual(self.l.evaluate_ticket_readiness(b).status,'missing_dependency')
 def test_missing_invalid_binding_and_origin_equivalence(self):
  x=self.l.create_ticket(title='x',state=CanonicalState.DRAFT,contract=contract());self.assertEqual(self.l.evaluate_ticket_readiness(x).status,'missing_runtime_binding');self.l.bind_runtime(x,'','');self.assertEqual(self.l.evaluate_ticket_readiness(x).status,'invalid_runtime_binding');rows=[self.ticket() for _ in range(3)];self.assertEqual([self.l.evaluate_ticket_readiness(r).status for r in rows],['ready']*3)
