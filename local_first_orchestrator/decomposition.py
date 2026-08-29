from __future__ import annotations
import hashlib,json
from dataclasses import dataclass
from .readiness import validate_ticket
from .ticket import MicroTicket
from .ledger import Ledger
import time
@dataclass(frozen=True)
class Criterion: id:str; statement:str; verification_hint:str=""
@dataclass(frozen=True)
class FeatureContract:
 id:str; title:str; objective:str; acceptance_criteria:tuple[Criterion,...]; non_goals:tuple[str,...]; invariants:tuple[str,...]; constraints:tuple[str,...]; source_revision:str
 @property
 def contract_hash(self):
  body={"id":self.id,"title":self.title,"objective":self.objective,"acceptance_criteria":[c.__dict__ for c in self.acceptance_criteria],"non_goals":self.non_goals,"invariants":self.invariants,"constraints":self.constraints,"source_revision":self.source_revision}
  return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),default=list).encode()).hexdigest()
@dataclass(frozen=True)
class Tranche:
 id:str; ordinal:int; objective:str; capabilities:tuple[str,...]; criterion_ids:tuple[str,...]; microtickets:tuple[MicroTicket,...]=()
@dataclass(frozen=True)
class DecompositionPlan:
 plan_version:int; feature_id:str; feature_contract_hash:str; repo_base_sha:str; repo_snapshot_hash:str; architecture_decisions:tuple[str,...]; criterion_coverage:dict[str,tuple[str,...]]; tranches:tuple[Tranche,...]; scope_change_proposals:tuple[str,...]=(); unresolved_questions:tuple[str,...]=()
@dataclass(frozen=True)
class PlanValidationResult: passed:bool; reasons:tuple[str,...]
class PlanValidator:
 def __init__(self,*,max_active_tickets:int=4,max_files:int=3,max_lines:int=200): self.max_active_tickets,self.max_files,self.max_lines=max_active_tickets,max_files,max_lines
 def validate(self,feature:FeatureContract,plan:DecompositionPlan)->PlanValidationResult:
  r=[]; criteria={c.id for c in feature.acceptance_criteria}
  if plan.feature_id!=feature.id:r.append('feature_mismatch')
  if plan.feature_contract_hash!=feature.contract_hash:r.append('stale_feature_contract')
  covered=set().union(*[set(x) for x in plan.criterion_coverage.values()]) if plan.criterion_coverage else set()
  if not criteria<=covered:r.append('criterion_uncovered')
  active=next((x for x in plan.tranches if x.ordinal==0),None)
  if active is None:r.append('invalid_microticket'); return PlanValidationResult(False,tuple(r))
  if len(active.microtickets)>self.max_active_tickets:r.append('active_tranche_limit_exceeded')
  ids={x.ticket_id for x in active.microtickets}
  for t in plan.tranches:
   if not set(t.criterion_ids)<=criteria:r.append('unknown_criterion')
  for t in active.microtickets:
   try: validate_ticket(t)
   except Exception:r.append('invalid_microticket'); continue
   if not set(t.criterion_ids)<=criteria:r.append('unknown_criterion')
   if t.patch_budget.max_files>self.max_files or t.patch_budget.max_changed_lines>self.max_lines:r.append('patch_budget_exceeded')
   if not set(t.dependencies)<=ids or t.ticket_id in t.dependencies:r.append('invalid_dependency')
  graph={t.ticket_id:set(t.dependencies) for t in active.microtickets}
  seen=set(); visiting=set()
  def dfs(n):
   if n not in graph:return False
   if n in visiting:return True
   if n in seen:return False
   seen.add(n);visiting.add(n); bad=any(dfs(x) for x in graph[n]);visiting.remove(n);return bad
  if any(dfs(n) for n in graph):r.append('dependency_cycle')
  if plan.scope_change_proposals and any('scope-change' in t.objective for t in active.microtickets):r.append('scope_expansion')
  if plan.unresolved_questions and active.microtickets:r.append('unresolved_choice')
  return PlanValidationResult(not r,tuple(sorted(set(r))))
def activate_validated_plan(ledger:Ledger,feature:FeatureContract,plan:DecompositionPlan,validation:PlanValidationResult)->tuple[str,tuple[str,...]]:
 if not validation.passed: raise ValueError('rejected plan cannot activate')
 raw=json.dumps({"feature":feature.__dict__,"plan":plan.__dict__},default=lambda x:x.__dict__ if hasattr(x,'__dict__') else list(x),sort_keys=True,separators=(',',':')); fp=hashlib.sha256(raw.encode()).hexdigest(); pid='plan-'+fp[:16]; now=int(time.time())
 with ledger._transaction() as c:
  old=c.execute('SELECT contract_hash FROM feature_contracts WHERE feature_id=?',(feature.id,)).fetchone()
  if old and old['contract_hash']!=feature.contract_hash: raise ValueError('conflicting feature contract')
  existing=c.execute('SELECT id FROM decomposition_plans WHERE fingerprint=?',(fp,)).fetchone()
  if existing:return str(existing['id']),tuple(r['id'] for r in c.execute('SELECT id FROM tickets WHERE feature_id=? AND tranche_id IN (SELECT id FROM tranches WHERE feature_id=? AND ordinal=0)',(feature.id,feature.id)))
  c.execute('INSERT OR IGNORE INTO features(id,title,objective,status,created_at,updated_at) VALUES (?,?,?,"planned",?,?)',(feature.id,feature.title,feature.objective,now,now)); c.execute('INSERT INTO feature_contracts VALUES (?,?,?,?)',(feature.id,feature.contract_hash,json.dumps(feature.__dict__,default=lambda x:x.__dict__,sort_keys=True),now)); c.execute('INSERT INTO decomposition_plans VALUES (?,?,?,?,?,?,?)',(pid,feature.id,fp,raw,'active',now,now))
  active=[]
  for tr in plan.tranches:
   c.execute('INSERT INTO tranches(id,feature_id,ordinal,status,integration_commands_json) VALUES (?,?,?, ?,"[]")',(tr.id,feature.id,tr.ordinal,'active' if tr.ordinal==0 else 'planned'))
   for cid in tr.criterion_ids:c.execute('INSERT INTO tranche_criteria VALUES (?,?)',(tr.id,cid))
   if tr.ordinal:
    continue
   for t in tr.microtickets:
    q=t.contract(); c.execute('INSERT INTO tickets(id,feature_id,tranche_id,title,objective,criterion_ids_json,primary_symbol,allowed_files_json,forbidden_changes_json,patch_budget_json,verification_json,risk,review_required,max_attempts,dependencies_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(t.ticket_id,feature.id,tr.id,t.ticket_id,q['objective'],json.dumps(q['criterion_ids']),q['primary_symbol'],json.dumps(q['allowed_files']),json.dumps(q['forbidden_changes']),json.dumps(q['patch_budget']),json.dumps(q['verification']),q['risk'],int(t.review_required),t.max_attempts,json.dumps(q['dependencies']),'draft',now,now)); active.append(t.ticket_id)
    for cid in t.criterion_ids:c.execute('INSERT INTO ticket_criteria VALUES (?,?)',(t.ticket_id,cid))
 return pid,tuple(active)
