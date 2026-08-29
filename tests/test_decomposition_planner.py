import json,subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.decomposition_planner import LocalDecompositionPlanner,packet
from local_first_orchestrator.repository_snapshot import RepositorySnapshot,ManifestEntry,Evidence
from tests.test_decomposition import Plans
class Planner(unittest.TestCase):
 def test_fake_process_parses_packet_and_persists_artifacts(self):
  p=Plans();f=p.feature();s=RepositorySnapshot('r','a'*40,(ManifestEntry('a.py','source'),),(Evidence('a.py','h','feature',('f',),1),));plan=p.plan(repo_base_sha=s.base_sha,repo_snapshot_hash=s.snapshot_hash); raw=json.dumps({'plan_version':plan.plan_version,'feature_id':plan.feature_id,'feature_contract_hash':plan.feature_contract_hash,'repo_base_sha':plan.repo_base_sha,'repo_snapshot_hash':plan.repo_snapshot_hash,'architecture_decisions':list(plan.architecture_decisions),'criterion_coverage':plan.criterion_coverage,'tranches':[{'id':t.id,'ordinal':t.ordinal,'objective':t.objective,'capabilities':list(t.capabilities),'criterion_ids':list(t.criterion_ids),'microtickets':[x.contract()|{'id':x.ticket_id} for x in t.microtickets]} for t in plan.tranches]})
  calls=[]
  def run(argv,**kw):calls.append(argv);return subprocess.CompletedProcess(argv,0,raw,'')
  d=TemporaryDirectory();got=LocalDecompositionPlanner(run).propose(f,s,artifact_dir=Path(d.name));self.assertEqual(got.feature_id,'F');self.assertIn('do not invent criteria',packet(f,s));self.assertTrue((Path(d.name)/'planner-response.json').exists());d.cleanup()
if __name__=='__main__':unittest.main()
