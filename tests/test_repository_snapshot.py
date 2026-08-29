import subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.repository_snapshot import snapshot
from local_first_orchestrator.decomposition import FeatureContract,Criterion
class Snap(unittest.TestCase):
 def test_feature_relevance_beats_tree_order(self):
  d=TemporaryDirectory();r=Path(d.name);subprocess.run(('git','init','-b','main'),cwd=r,check=True,capture_output=True);subprocess.run(('git','config','user.email','x@y'),cwd=r,check=True);subprocess.run(('git','config','user.name','x'),cwd=r,check=True)
  for i in range(40):(r/f'a{i:02}.py').write_text('def unrelated(): pass\n')
  (r/'z_payment.py').write_text('def calculate_payment(): pass\n');subprocess.run(('git','add','.'),cwd=r,check=True);subprocess.run(('git','commit','-m','a'),cwd=r,check=True,capture_output=True);sha=subprocess.run(('git','rev-parse','HEAD'),cwd=r,text=True,capture_output=True,check=True).stdout.strip();f=FeatureContract('f','Payments','Calculate payment totals',(Criterion('A','payment total'),),(),(),(),sha);one=snapshot(r,sha,f,limit=2);self.assertIn('z_payment.py',[x.path for x in one.entries]);(r/'z_payment.py').write_text('dirty');self.assertEqual(one,snapshot(r,sha,f,limit=2));d.cleanup()
