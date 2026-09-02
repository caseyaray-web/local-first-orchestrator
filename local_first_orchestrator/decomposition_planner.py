from __future__ import annotations
import json,hashlib,subprocess
from pathlib import Path
from .decomposition import FeatureContract,DecompositionPlan,Tranche
from .ticket import MicroTicket,PatchBudget,VerificationProfile
from .repository_snapshot import RepositorySnapshot
class PlannerError(RuntimeError):pass
def packet(feature:FeatureContract,snapshot:RepositorySnapshot,*,max_active:int=4,max_files:int=3,max_lines:int=200,prior_decisions:tuple[str,...]=())->str:
 return json.dumps({'feature':{**feature.__dict__,'contract_hash':feature.contract_hash},'repository':{'id':snapshot.repository_id,'base_sha':snapshot.base_sha,'snapshot_hash':snapshot.snapshot_hash,'manifest':[x.__dict__ for x in snapshot.manifest],'evidence':[x.__dict__ for x in snapshot.entries],'omitted_count':snapshot.omitted_count},'limits':{'active':max_active,'max_files':max_files,'max_lines':max_lines},'prior_decisions':prior_decisions,'rules':['do not invent criteria or broaden scope','use supplied repository-relative path::symbol only','materialize active tranche only','output JSON only']},sort_keys=True,separators=(',',':'),default=lambda x:x.__dict__ if hasattr(x,'__dict__') else list(x))
def _ticket(x):
 v=x['verification'];return MicroTicket(x.get('id',x.get('ticket_id')),x['objective'],tuple(x['criterion_ids']),x['primary_symbol'],tuple(x['allowed_files']),tuple(x['forbidden_changes']),PatchBudget(**x['patch_budget']),VerificationProfile(tuple(tuple(a) for a in v['commands']),v.get('working_directory','.')) ,x['risk'],x['review_required'],x['max_attempts'],tuple(x.get('dependencies',())),tuple(x.get('new_test_files',())))
def parse(raw:str)->DecompositionPlan:
 try:x=json.loads(raw)
 except json.JSONDecodeError as e:raise PlannerError('malformed planner JSON') from e
 try:
  return DecompositionPlan(x['plan_version'],x['feature_id'],x['feature_contract_hash'],x['repo_base_sha'],x['repo_snapshot_hash'],tuple(x['architecture_decisions']),{k:tuple(v) for k,v in x['criterion_coverage'].items()},tuple(Tranche(t['id'],t['ordinal'],t['objective'],tuple(t['capabilities']),tuple(t['criterion_ids']),tuple(_ticket(y) for y in t.get('microtickets',()))) for t in x['tranches']),tuple(x.get('scope_change_proposals',())),tuple(x.get('unresolved_questions',())),x.get('repository_identity',''),x.get('repo_snapshot_manifest_json',''))
 except (KeyError,TypeError,ValueError) as e:raise PlannerError('invalid planner schema') from e
class LocalDecompositionPlanner:
 def __init__(self,runner=subprocess.run,executable='hermes',cost_class:str='unknown'):
  if cost_class not in {'local','paid','unknown'}:raise ValueError('invalid planner cost class')
  self.runner,self.executable,self.cost_class=runner,executable,cost_class
 @property
 def is_paid(self):return self.cost_class=='paid'
 def propose(self,feature,snapshot,*,artifact_dir:Path,prior_decisions=()):
  if self.cost_class=='unknown':raise PlannerError('unknown planner cost class')
  p=packet(feature,snapshot,prior_decisions=prior_decisions);artifact_dir.mkdir(parents=True,exist_ok=True)
  try:r=self.runner((self.executable,'chat','--query',p,'--quiet'),text=True,capture_output=True,timeout=300,check=False)
  except subprocess.TimeoutExpired as e:raise PlannerError('planner timeout') from e
  (artifact_dir/'planner-request.json').write_text(p);(artifact_dir/'planner-response.json').write_text(r.stdout or '')
  if r.returncode:raise PlannerError('planner failure')
  if not (r.stdout or '').strip():raise PlannerError('empty planner response')
  return parse(r.stdout)
