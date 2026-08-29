from __future__ import annotations
import hashlib,json,subprocess
from dataclasses import dataclass
from pathlib import Path
from .decomposition import DecompositionPlan
@dataclass(frozen=True)
class Evidence: path:str; content_hash:str; reason:str; symbols:tuple[str,...]
@dataclass(frozen=True)
class RepositorySnapshot:
 repository_id:str;base_sha:str;entries:tuple[Evidence,...]
 @property
 def snapshot_hash(self):return hashlib.sha256(json.dumps({'repository':self.repository_id,'base':self.base_sha,'entries':[x.__dict__ for x in self.entries]},sort_keys=True,separators=(',',':')).encode()).hexdigest()
def snapshot(repository:Path,requested_sha:str,feature_terms:tuple[str,...]=(),limit:int=32)->RepositorySnapshot:
 repo=Path(repository).resolve(); run=lambda *a:subprocess.run(('git',*a),cwd=repo,text=True,capture_output=True,check=True).stdout
 base=run('rev-parse','--verify',requested_sha+'^{commit}').strip(); paths=run('ls-tree','-r','--name-only',base).splitlines(); chosen=[p for p in paths if p.endswith('.py')][:limit]
 out=[]
 for p in chosen:
  data=run('show',base+':'+p); sy=tuple(line.split('def ',1)[1].split('(',1)[0] for line in data.splitlines() if line.startswith('def '));out.append(Evidence(p,hashlib.sha256(data.encode()).hexdigest(),'source',sy))
 return RepositorySnapshot(repo.name,base,tuple(out))
@dataclass(frozen=True)
class RepositoryValidation: passed:bool;reasons:tuple[str,...]
class RepositoryPlanValidator:
 def validate(self,plan:DecompositionPlan,s:RepositorySnapshot)->RepositoryValidation:
  r=[]
  if plan.repo_base_sha!=s.base_sha:r.append('repo_base_mismatch')
  if plan.repo_snapshot_hash!=s.snapshot_hash:r.append('stale_repo_snapshot')
  files={x.path:x for x in s.entries}; active=next((x for x in plan.tranches if x.ordinal==0),None)
  if active:
   for t in active.microtickets:
    for p in t.allowed_files:
     if p.startswith('/') or '..' in Path(p).parts or p not in files:r.append('unknown_file')
    try:p,n=t.primary_symbol.split('::',1); ok=p in files and n in files[p].symbols
    except ValueError:ok=False
    if not ok:r.append('unknown_symbol')
  return RepositoryValidation(not r,tuple(sorted(set(r))))
