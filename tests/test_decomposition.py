import unittest
from local_first_orchestrator.decomposition import *
from local_first_orchestrator.ticket import MicroTicket,PatchBudget,VerificationProfile
def ticket(i,criteria=('A',),deps=()):return MicroTicket(i,'Return fixture value.',criteria,'a.py::f',('a.py',),('No API change.',),PatchBudget(1,10),VerificationProfile((('python','-c','print(1)'),)),'low',True,2,deps)
class Plans(unittest.TestCase):
 def feature(self):return FeatureContract('F','Feature','objective',(Criterion('A','a'),Criterion('B','b')),("no",),("safe",),(),"sha")
 def plan(self,**kw):
  f=self.feature(); tr=(Tranche('now',0,'now',('c',),('A','B'),(ticket('one',('A',)),ticket('two',('B',),('one',)))),Tranche('later',1,'later',('c',),('B',),()))
  d=dict(plan_version=1,feature_id='F',feature_contract_hash=f.contract_hash,repo_base_sha='a',repo_snapshot_hash='b',architecture_decisions=('d',),criterion_coverage={'now':('A','B')},tranches=tr);d.update(kw);return DecompositionPlan(**d)
 def test_valid_hash_and_coarse_future(self):
  f=self.feature();self.assertEqual(f.contract_hash,self.feature().contract_hash);self.assertTrue(PlanValidator().validate(f,self.plan()).passed)
 def test_rejections(self):
  f=self.feature()
  for p,reason in [(self.plan(feature_contract_hash='old'),'stale_feature_contract'),(self.plan(criterion_coverage={'now':('A',)}),'criterion_uncovered'),(self.plan(tranches=(Tranche('n',0,'n',(),('A',),(ticket('x',('A',),('bad',)),)),)),'invalid_dependency')]:self.assertIn(reason,PlanValidator().validate(f,p).reasons)
if __name__=='__main__':unittest.main()
