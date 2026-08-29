from __future__ import annotations
import hashlib,json
from dataclasses import dataclass
from .readiness import validate_ticket
from .ticket import MicroTicket
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
