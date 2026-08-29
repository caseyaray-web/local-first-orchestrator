from __future__ import annotations
import hashlib,json,re,subprocess
from dataclasses import dataclass
from pathlib import Path
from .decomposition import DecompositionPlan,FeatureContract
_STOP={'the','and','for','with','from','that','this','into','only','must','shall','are','not'}
@dataclass(frozen=True)
class Evidence: path:str; content_hash:str; reason:str; symbols:tuple[str,...]; score:int=0
@dataclass(frozen=True)
class ManifestEntry: path:str; kind:str
@dataclass(frozen=True)
class RepositorySnapshot:
 repository_id:str;base_sha:str;manifest:tuple[ManifestEntry,...];entries:tuple[Evidence,...];omitted_count:int=0
 @property
 def snapshot_hash(self):return hashlib.sha256(json.dumps({'repository':self.repository_id,'base':self.base_sha,'manifest':[x.__dict__ for x in self.manifest],'entries':[x.__dict__ for x in self.entries],'omitted':self.omitted_count},sort_keys=True,separators=(',',':')).encode()).hexdigest()
def terms(feature:FeatureContract|None,extra:tuple[str,...])->tuple[str,...]:
 text=' '.join(extra) if feature is None else ' '.join((feature.title,feature.objective,*[x.statement for x in feature.acceptance_criteria]))
 return tuple(sorted({x.lower() for x in re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}',text) if x.lower() not in _STOP}))
def snapshot(repository:Path,requested_sha:str,feature:FeatureContract|None=None,feature_terms:tuple[str,...]=(),limit:int=32)->RepositorySnapshot:
 repo=Path(repository).resolve(); run=lambda *a:subprocess.run(('git',*a),cwd=repo,text=True,capture_output=True,check=True).stdout
 base=run('rev-parse','--verify',requested_sha+'^{commit}').strip(); paths=[p for p in run('ls-tree','-r','--name-only',base).splitlines() if p.endswith('.py')]
 manifest=tuple(ManifestEntry(p,'test' if Path(p).name.startswith('test_') or '/tests/' in p else 'source') for p in paths); q=terms(feature,feature_terms); candidates=[]
 for m in manifest:
  data=run('show',base+':'+m.path); lower=data.lower(); score=sum(3 for x in q if x in Path(m.path).name.lower())+sum(1 for x in q if x in lower)
  if score or not q:candidates.append((m.path,score,data))
 chosen=sorted(candidates,key=lambda x:(-x[1],x[0]))[:limit]; out=[]
 for p,score,data in chosen:
  sy=tuple(line.split('def ',1)[1].split('(',1)[0] for line in data.splitlines() if line.startswith('def '));out.append(Evidence(p,hashlib.sha256(data.encode()).hexdigest(),'feature_term_match',sy,score))
 return RepositorySnapshot(repo.name,base,manifest,tuple(out),max(0,len(candidates)-len(chosen)))
@dataclass(frozen=True)
class RepositoryValidation: passed:bool;reasons:tuple[str,...]
class RepositoryPlanValidator:
 def validate(self,plan:DecompositionPlan,s:RepositorySnapshot)->RepositoryValidation:
  r=[]
  if plan.repo_base_sha!=s.base_sha:r.append('repo_base_mismatch')
  if plan.repo_snapshot_hash!=s.snapshot_hash:r.append('stale_repo_snapshot')
  manifest={x.path for x in s.manifest}; evidence={x.path:x for x in s.entries}; active=next((x for x in plan.tranches if x.ordinal==0),None)
  if active:
   for t in active.microtickets:
    for p in t.allowed_files:
     if p.startswith('/') or '..' in Path(p).parts or p not in manifest:r.append('unknown_file')
    try:p,n=t.primary_symbol.split('::',1); ok=p in evidence and n in evidence[p].symbols
    except ValueError:ok=False
    if not ok:r.append('insufficient_repository_evidence' if p in manifest else 'unknown_symbol')
  return RepositoryValidation(not r,tuple(sorted(set(r))))
