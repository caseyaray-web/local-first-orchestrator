import json
import hashlib
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.decomposition import *
from local_first_orchestrator.ticket import MicroTicket,PatchBudget,VerificationProfile
from local_first_orchestrator.repository_snapshot import canonical_json
def ticket(i,criteria=('A',),deps=()):return MicroTicket(i,'Return fixture value.',criteria,'a.py::f',('a.py',),('No API change.',),PatchBudget(1,10),VerificationProfile((('python','-c','print(1)'),)),'low',True,2,deps)
class Plans(unittest.TestCase):
 def feature(self):return FeatureContract('F','Feature','objective',(Criterion('A','a'),Criterion('B','b')),("no",),("safe",),(),"sha")
 def plan(self,**kw):
  f=self.feature(); tr=(Tranche('now',0,'now',('c',),('A','B'),(ticket('one',('A',)),ticket('two',('B',),('one',)))),Tranche('later',1,'later',('c',),('B',),(ticket('two',('B',)),)))
  manifest=canonical_json({'repository_identity':'fixture-repo','repo_base_sha':'a','manifest':[],'evidence':[],'omitted_count':0})
  d=dict(plan_version=1,feature_id='F',feature_contract_hash=f.contract_hash,repo_base_sha='a',repo_snapshot_hash=hashlib.sha256(manifest.encode()).hexdigest(),architecture_decisions=('d',),criterion_coverage={'A':('one',),'B':('two',)},tranches=tr,repository_identity='fixture-repo',repo_snapshot_manifest_json=manifest);d.update(kw);return DecompositionPlan(**d)
 def repository_validation(self,plan):
  return type('R',(),{'passed':True,'repository_identity':plan.repository_identity,'base_sha':plan.repo_base_sha,'snapshot_hash':plan.repo_snapshot_hash,'manifest_json':plan.repo_snapshot_manifest_json})()
 def test_valid_hash_and_coarse_future(self):
  f=self.feature();self.assertEqual(f.contract_hash,self.feature().contract_hash);self.assertTrue(PlanValidator().validate(f,self.plan()).passed)
 def test_rejections(self):
  f=self.feature()
  for p,reason in [(self.plan(feature_contract_hash='old'),'stale_feature_contract'),(self.plan(criterion_coverage={'A':('one',)}),'criterion_uncovered'),(self.plan(tranches=(Tranche('n',0,'n',(),('A',),(ticket('x',('A',),('bad',)),)),)),'invalid_dependency')]:self.assertIn(reason,PlanValidator().validate(f,p).reasons)
 def test_activation_persists_active_generated_tickets_and_create_projections(self):
  d=TemporaryDirectory(); l=Ledger(Path(d.name)/'x.db');l.migrate(); f=self.feature();p=self.plan();v=PlanValidator().validate(f,p); ok=self.repository_validation(p); ticket_ids=activate_validated_plan(l,f,p,v,ok)[1]
  self.assertEqual(len(ticket_ids), 2)
  rows=l.connection.execute("select * from board_projection_outbox where operation='create_microticket' order by ticket_id").fetchall()
  self.assertEqual(len(rows), 2)
  for row in rows:
   t=l.get_ticket(row['ticket_id']); payload=json.loads(row['payload_json']); self.assertEqual(t['state'], 'draft'); self.assertEqual(payload['orchestrator_ticket_id'], t['id']); self.assertEqual(payload['feature_id'], 'F'); self.assertEqual(payload['tranche_id'], t['tranche_id']); self.assertEqual(payload['projection_key'], 'board-create:v1:'+t['id']); self.assertEqual(row['idempotency_key'], payload['projection_key']); self.assertIsNone(row['acknowledged_at']); self.assertEqual(row['attempt_count'], 0); self.assertIsNone(row['lease_owner']); self.assertIsNone(row['external_task_id']); self.assertIn('<!-- local-first-orchestrator -->', payload['body']); self.assertIn('```local-first-contract', payload['body']); contract=json.loads(payload['body'].split('```local-first-contract\\n',1)[1].split('\\n```',1)[0]); self.assertNotIn('projection_generation',contract)
  self.assertEqual(l.connection.execute("select count(*) from board_projection_outbox where operation='create_microticket' and event_id in (select id from events where event_type='generated_microticket_created')").fetchone()[0], 2); l.close();d.cleanup()

 def test_activation_is_atomic_and_idempotent(self):
  d=TemporaryDirectory(); l=Ledger(Path(d.name)/'x.db');l.migrate(); f=self.feature();p=self.plan();v=PlanValidator().validate(f,p); ok=self.repository_validation(p); one=activate_validated_plan(l,f,p,v,ok);two=activate_validated_plan(l,f,p,v,ok);self.assertEqual(one,two);self.assertEqual(l.connection.execute('select count(*) n from tickets').fetchone()['n'],2);self.assertEqual(l.connection.execute('select count(*) n from tranches').fetchone()['n'],2);self.assertEqual(l.connection.execute("select count(*) from board_projection_outbox where operation='create_microticket'").fetchone()[0],2);l.close();d.cleanup()

 def test_activation_rolls_back_ticket_and_projection_pair(self):
  d=TemporaryDirectory(); calls=[]
  def fail(point):
   calls.append(point)
   if point == 'after_generated_projection': raise RuntimeError(point)
  l=Ledger(Path(d.name)/'x.db', failure_injector=fail);l.migrate(); f=self.feature();p=self.plan();v=PlanValidator().validate(f,p); ok=self.repository_validation(p);
  with self.assertRaisesRegex(RuntimeError, 'after_generated_projection'): activate_validated_plan(l,f,p,v,ok)
  self.assertEqual(l.connection.execute('select count(*) from tickets').fetchone()[0], 0); self.assertEqual(l.connection.execute("select count(*) from board_projection_outbox where operation='create_microticket'").fetchone()[0], 0); self.assertEqual(l.connection.execute("select count(*) from events where event_type='generated_microticket_created'").fetchone()[0], 0); l.close();d.cleanup()

 def test_future_tranches_are_not_projected_or_materialized(self):
  d=TemporaryDirectory(); l=Ledger(Path(d.name)/'x.db');l.migrate(); f=self.feature(); base=self.plan(); p=self.plan(tranches=(base.tranches[0], Tranche('later',1,'later',('c',),('B',),(ticket('future',('B',)),)))); v=PlanValidator().validate(f,p); ok=self.repository_validation(p); activate_validated_plan(l,f,p,v,ok); self.assertEqual(l.connection.execute("select count(*) from tickets where tranche_id='later'").fetchone()[0],0); self.assertEqual(l.connection.execute("select count(*) from board_projection_outbox where operation='create_microticket'").fetchone()[0],2); l.close();d.cleanup()

 def test_replay_conflict_fails_closed(self):
  d=TemporaryDirectory(); l=Ledger(Path(d.name)/'x.db');l.migrate(); f=self.feature();p=self.plan();v=PlanValidator().validate(f,p); ok=self.repository_validation(p); activate_validated_plan(l,f,p,v,ok); row=l.connection.execute("select ticket_id,event_id from board_projection_outbox where operation='create_microticket' order by ticket_id limit 1").fetchone(); l.connection.execute("update board_projection_outbox set payload_json='{}' where ticket_id=? and event_id=?", (row['ticket_id'], row['event_id']))
  with self.assertRaisesRegex(ValueError, 'conflicts'): activate_validated_plan(l,f,p,v,ok)
  l.close();d.cleanup()
