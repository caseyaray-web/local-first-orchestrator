import subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.repository_snapshot import snapshot,RepositoryPlanValidator
from tests.test_decomposition import Plans
class Snap(unittest.TestCase):
 def test_commit_snapshot_ignores_dirty_checkout(self):
  d=TemporaryDirectory();r=Path(d.name);subprocess.run(('git','init','-b','main'),cwd=r,check=True,capture_output=True);subprocess.run(('git','config','user.email','x@y'),cwd=r,check=True);subprocess.run(('git','config','user.name','x'),cwd=r,check=True);(r/'a.py').write_text('def f(): pass\n');subprocess.run(('git','add','.'),cwd=r,check=True);subprocess.run(('git','commit','-m','a'),cwd=r,check=True,capture_output=True);sha=subprocess.run(('git','rev-parse','HEAD'),cwd=r,text=True,capture_output=True,check=True).stdout.strip();one=snapshot(r,sha);(r/'a.py').write_text('def changed():pass\n');self.assertEqual(one,snapshot(r,sha));p=Plans();plan=p.plan(repo_base_sha=sha,repo_snapshot_hash=one.snapshot_hash);self.assertTrue(RepositoryPlanValidator().validate(plan,one).passed);d.cleanup()
