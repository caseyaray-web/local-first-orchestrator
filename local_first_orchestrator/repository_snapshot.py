from __future__ import annotations
import hashlib,json,re,subprocess
from dataclasses import dataclass
from pathlib import Path
from .decomposition import DecompositionPlan,FeatureContract
from .source_languages import is_supported_repository_file,is_supported_source,is_test_path,normalized_repository_path
from .symbols import symbols_for
_STOP={'the','and','for','with','from','that','this','into','only','must','shall','are','not'}
@dataclass(frozen=True)
class Evidence: path:str; content_hash:str; reason:str; symbols:tuple[str,...]; score:int=0
@dataclass(frozen=True)
class ManifestEntry: path:str; kind:str
@dataclass(frozen=True)
class RepositorySnapshot:
 repository_id:str;base_sha:str;manifest:tuple[ManifestEntry,...];entries:tuple[Evidence,...];omitted_count:int=0;authorized_modify_paths:tuple[str,...]=();authorized_create_paths:tuple[str,...]=();contract_bound:bool=False
 @property
 def manifest_payload(self):
  return {'repository_identity':self.repository_id,'repo_base_sha':self.base_sha,'manifest':[x.__dict__ for x in self.manifest],'evidence':[x.__dict__ for x in self.entries],'omitted_count':self.omitted_count,'authorized_modify_paths':list(self.authorized_modify_paths),'authorized_create_paths':list(self.authorized_create_paths),'contract_bound':self.contract_bound}
 @property
 def manifest_json(self):return canonical_json(self.manifest_payload)
 @property
 def snapshot_hash(self):return hashlib.sha256(self.manifest_json.encode()).hexdigest()
def canonical_json(value:object)->str:return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
def terms(feature:FeatureContract|None,extra:tuple[str,...])->tuple[str,...]:
 text=' '.join(extra) if feature is None else ' '.join((feature.title,feature.objective,*[x.statement for x in feature.acceptance_criteria]))
 return tuple(sorted({x.lower() for x in re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}',text) if x.lower() not in _STOP}))
def snapshot(repository:Path,requested_sha:str,feature:FeatureContract|None=None,feature_terms:tuple[str,...]=(),limit:int=32,authorized_modify_paths:tuple[str,...]=(),authorized_create_paths:tuple[str,...]=())->RepositorySnapshot:
 repo=Path(repository).resolve(); run=lambda *a:subprocess.run(('git',*a),cwd=repo,text=True,capture_output=True,check=True).stdout
 base=run('rev-parse','--verify',requested_sha+'^{commit}').strip(); paths=[p for p in run('ls-tree','-r','--name-only',base).splitlines() if is_supported_repository_file(p)]
 manifest=tuple(ManifestEntry(p,'test' if is_test_path(p) else 'source') for p in paths); manifest_paths=set(paths); required=tuple(sorted(set(authorized_modify_paths)))
 if len(required)>limit: raise ValueError('authorized modify evidence exceeds evidence limit')
 required_entries=[]
 for p in required:
  if normalized_repository_path(p) is None or p not in manifest_paths: raise ValueError(f'authorized modify path is not a supported base blob: {p}')
  data=run('show',base+':'+p); required_entries.append(Evidence(p,hashlib.sha256(data.encode()).hexdigest(),'authorized_modify_target',symbols_for(p,data),10**9))
 q=terms(feature,feature_terms); candidates=[]
 for m in manifest:
  data=run('show',base+':'+m.path); lower=data.lower(); score=sum(3 for x in q if x in Path(m.path).name.lower())+sum(1 for x in q if x in lower)
  if m.path not in required and (score or not q):candidates.append((m.path,score,data))
 chosen=sorted(candidates,key=lambda x:(-x[1],x[0]))[:limit-len(required)]; out=list(required_entries)
 for p,score,data in chosen:
  out.append(Evidence(p,hashlib.sha256(data.encode()).hexdigest(),'feature_term_match',symbols_for(p,data),score))
 return RepositorySnapshot(str(repo),base,manifest,tuple(out),max(0,len(candidates)-len(chosen)),required,tuple(sorted(set(authorized_create_paths))),bool(authorized_modify_paths or authorized_create_paths))
@dataclass(frozen=True)
class RepositoryValidation:
 passed:bool;reasons:tuple[str,...];repository_identity:str="";base_sha:str="";snapshot_hash:str="";manifest_json:str=""
class RepositoryPlanValidator:
 def validate(self,plan:DecompositionPlan,s:RepositorySnapshot)->RepositoryValidation:
  r=[]
  if not plan.repository_identity or plan.repository_identity!=s.repository_id:r.append('repository_identity_mismatch')
  if plan.repo_base_sha!=s.base_sha:r.append('repo_base_mismatch')
  if plan.repo_snapshot_hash!=s.snapshot_hash:r.append('stale_repo_snapshot')
  if not plan.repo_snapshot_manifest_json or plan.repo_snapshot_manifest_json!=s.manifest_json:r.append('stale_repo_snapshot')
  manifest={x.path for x in s.manifest}; evidence={x.path:x for x in s.entries}; active=next((x for x in plan.tranches if x.ordinal==0),None); modify=set(s.authorized_modify_paths); create=set(s.authorized_create_paths)
  if active:
   for t in active.microtickets:
    for p in t.allowed_files:
     if normalized_repository_path(p) is None or p not in manifest or (s.contract_bound and p not in modify):r.append('unknown_file')
    for p in t.create_files:
     if normalized_repository_path(p) is None or (s.contract_bound and p not in create):r.append('unknown_create_file')
     elif p in manifest:r.append('create_file_already_exists')
     elif not is_supported_source(p) or is_test_path(p):r.append('create_file_not_source')
    for p in t.new_test_files:
     if normalized_repository_path(p) is None:r.append('invalid_new_test_file')
     elif s.contract_bound and p not in create:r.append('unknown_create_test_file')
     elif p in manifest:r.append('new_test_file_already_exists')
     elif not is_supported_source(p) or not is_test_path(p):r.append('new_test_file_not_test')
    p=''; n=''
    try:p,n=t.primary_symbol.split('::',1)
    except ValueError: pass
    if p not in manifest:r.append('unknown_symbol')
    elif p not in evidence:r.append('insufficient_repository_evidence')
    elif n not in evidence[p].symbols:r.append('unknown_symbol')
  return RepositoryValidation(not r,tuple(sorted(set(r))),s.repository_id,s.base_sha,s.snapshot_hash,s.manifest_json)
