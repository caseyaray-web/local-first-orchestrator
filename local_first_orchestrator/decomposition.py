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
 plan_version:int; feature_id:str; feature_contract_hash:str; repo_base_sha:str; repo_snapshot_hash:str; architecture_decisions:tuple[str,...]; criterion_coverage:dict[str,tuple[str,...]]; tranches:tuple[Tranche,...]; scope_change_proposals:tuple[str,...]=(); unresolved_questions:tuple[str,...]=(); repository_identity:str=""; repo_snapshot_manifest_json:str=""
@dataclass(frozen=True)
class PlanValidationResult: passed:bool; reasons:tuple[str,...]
class PlanValidator:
 def __init__(self,*,max_active_tickets:int=4,max_files:int=3,max_lines:int=200): self.max_active_tickets,self.max_files,self.max_lines=max_active_tickets,max_files,max_lines
 def validate_tranche(self,feature:FeatureContract,tranche:Tranche,coarse:Tranche)->PlanValidationResult:
  r=[]; criteria={c.id for c in feature.acceptance_criteria}; allowed=set(coarse.criterion_ids)
  if not tranche.microtickets:r.append('invalid_microticket')
  if not set(tranche.criterion_ids)<=allowed:r.append('tranche_scope_expanded')
  ids={x.ticket_id for x in tranche.microtickets}
  for t in tranche.microtickets:
   try: validate_ticket(t)
   except Exception:r.append('invalid_microticket'); continue
   if not set(t.criterion_ids)<=allowed or not set(t.criterion_ids)<=criteria:r.append('criterion_scope_expanded')
   if t.patch_budget.max_files>self.max_files or t.patch_budget.max_changed_lines>self.max_lines:r.append('patch_budget_exceeded')
   if not set(t.dependencies)<=ids or t.ticket_id in t.dependencies:r.append('invalid_dependency')
  graph={t.ticket_id:set(t.dependencies) for t in tranche.microtickets}; seen=set(); visiting=set()
  def dfs(n):
   if n in visiting:return True
   if n in seen:return False
   seen.add(n); visiting.add(n); bad=any(dfs(x) for x in graph.get(n,())) ; visiting.remove(n); return bad
  if any(dfs(n) for n in graph):r.append('dependency_cycle')
  return PlanValidationResult(not r,tuple(sorted(set(r))))
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

_LOCAL_FIRST_MARKER = "<!-- local-first-orchestrator -->"
_LOCAL_FIRST_CONTRACT = "local-first-contract"


def generated_projection_key(ticket_id: str) -> str:
 """Return the stable Hermes idempotency key for a generated ticket."""
 return f"board-create:v1:{ticket_id}"


def generated_card_payload(feature: FeatureContract, tranche: Tranche, ticket: MicroTicket, *, repository_identity: str, repo_base_sha: str, repo_snapshot_hash: str) -> dict[str, str]:
 """Serialize the immutable generated-card contract and projection payload."""
 projection_key = generated_projection_key(ticket.ticket_id)
 contract = {
  "kind": "microticket",
  "orchestrator_ticket_id": ticket.ticket_id,
  "feature_id": feature.id,
  "tranche_id": tranche.id,
  "projection_key": projection_key,
  "repository_identity": repository_identity,
  "repo_base_sha": repo_base_sha,
  "repo_snapshot_hash": repo_snapshot_hash,
  **ticket.contract(),
 }
 encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
 body = f"{_LOCAL_FIRST_MARKER}\\n```{_LOCAL_FIRST_CONTRACT}\\n{encoded}\\n```"
 return {
  "title": ticket.ticket_id,
  "body": body,
  "orchestrator_ticket_id": ticket.ticket_id,
  "feature_id": feature.id,
  "tranche_id": tranche.id,
  "projection_key": projection_key,
 }

def activate_validated_plan(ledger:Ledger,feature:FeatureContract,plan:DecompositionPlan,validation:PlanValidationResult,repository_validation:object|None=None)->tuple[str,tuple[str,...]]:
 if not validation.passed or repository_validation is None or not getattr(repository_validation,'passed',False): raise ValueError('rejected plan cannot activate')
 repository_identity=getattr(repository_validation,'repository_identity',None)
 repo_base_sha=getattr(repository_validation,'base_sha',None)
 repo_snapshot_hash=getattr(repository_validation,'snapshot_hash',None)
 repo_snapshot_manifest_json=getattr(repository_validation,'manifest_json',None)
 if not all((repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json)):
  raise ValueError('repository provenance required')
 try:
  manifest=json.loads(repo_snapshot_manifest_json)
 except (TypeError, json.JSONDecodeError) as exc:
  raise ValueError('repository provenance conflicts') from exc
 if (manifest.get('repository_identity'),manifest.get('repo_base_sha')) != (repository_identity,repo_base_sha) or hashlib.sha256(repo_snapshot_manifest_json.encode()).hexdigest()!=repo_snapshot_hash:
  raise ValueError('repository provenance conflicts')
 if (plan.repository_identity,plan.repo_base_sha,plan.repo_snapshot_hash,plan.repo_snapshot_manifest_json)!=(repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json):
  raise ValueError('repository provenance conflicts')
 raw=json.dumps({"feature":feature.__dict__,"plan":plan.__dict__},default=lambda x:x.__dict__ if hasattr(x,'__dict__') else list(x),sort_keys=True,separators=(',',':')); fp=hashlib.sha256(raw.encode()).hexdigest(); pid='plan-'+fp[:16]; now=int(time.time())
 with ledger._transaction() as c:
  old=c.execute('SELECT * FROM feature_contracts WHERE feature_id=?',(feature.id,)).fetchone()
  if old and old['contract_hash']!=feature.contract_hash: raise ValueError('conflicting feature contract')
  if old and any(old[key] is not None and old[key] != value for key,value in (("repository_identity",repository_identity),("repo_base_sha",repo_base_sha),("repo_snapshot_hash",repo_snapshot_hash),("repo_snapshot_manifest_json",repo_snapshot_manifest_json))): raise ValueError('repository provenance conflicts')
  existing=c.execute('SELECT * FROM decomposition_plans WHERE fingerprint=?',(fp,)).fetchone()
  if existing:
   if tuple(existing[x] for x in ('repository_identity','repo_base_sha','repo_snapshot_hash','repo_snapshot_manifest_json')) != (repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json): raise ValueError('repository provenance conflicts')
   active_tranche=next(tr for tr in plan.tranches if tr.ordinal == 0)
   if existing['status'] == 'validated_pending_activation':
    pass
   else:
    for generated in active_tranche.microtickets:
     expected=generated_card_payload(feature, active_tranche, generated, repository_identity=str(repository_identity), repo_base_sha=str(repo_base_sha), repo_snapshot_hash=str(repo_snapshot_hash))
     ticket_row=c.execute('SELECT id FROM tickets WHERE id=? AND feature_id=? AND tranche_id=? AND state=?',(generated.ticket_id,feature.id,active_tranche.id,'draft')).fetchone()
     event_row=c.execute("SELECT id FROM events WHERE entity_type='ticket' AND entity_id=? AND event_type='generated_microticket_created' ORDER BY id DESC LIMIT 1",(generated.ticket_id,)).fetchone()
     projection=c.execute('SELECT operation,payload_json,idempotency_key FROM board_projection_outbox WHERE ticket_id=? AND event_id=?',(generated.ticket_id,event_row['id'] if event_row else -1)).fetchone()
     if ticket_row is None or event_row is None or projection is None or (projection['operation'],projection['payload_json'],projection['idempotency_key']) != ('create_microticket',json.dumps(expected,sort_keys=True,separators=(',',':')),expected['projection_key']):
      raise ValueError('create projection conflicts')
   if existing['status'] != 'validated_pending_activation':
    return str(existing['id']),tuple(r['id'] for r in c.execute('SELECT id FROM tickets WHERE feature_id=? AND tranche_id IN (SELECT id FROM tranches WHERE feature_id=? AND ordinal=0)',(feature.id,feature.id)))
  for tr in plan.tranches:
   if tr.ordinal == 0:
    for generated in tr.microtickets:
     if c.execute('SELECT 1 FROM tickets WHERE id=?',(generated.ticket_id,)).fetchone() is not None:
      raise ValueError('conflicting activated plan')
  c.execute('INSERT OR IGNORE INTO features(id,title,objective,status,created_at,updated_at) VALUES (?,?,?,"planned",?,?)',(feature.id,feature.title,feature.objective,now,now))
  if old is None:
   c.execute('INSERT INTO feature_contracts(feature_id,contract_hash,contract_json,created_at) VALUES (?,?,?,?)',(feature.id,feature.contract_hash,json.dumps(feature.__dict__,default=lambda x:x.__dict__,sort_keys=True),now))
  if not existing:
   c.execute('INSERT INTO decomposition_plans(id,feature_id,fingerprint,plan_json,status,created_at,activated_at,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(pid,feature.id,fp,raw,'active',now,now,repository_identity,repo_base_sha,repo_snapshot_hash,repo_snapshot_manifest_json))
  active=[]
  for tr in plan.tranches:
   existing_tranche=c.execute('SELECT * FROM tranches WHERE id=?',(tr.id,)).fetchone()
   if existing_tranche:
    existing_criteria={r['criterion_id'] for r in c.execute('SELECT criterion_id FROM tranche_criteria WHERE tranche_id=?',(tr.id,))}
    if existing_tranche['feature_id']!=feature.id or int(existing_tranche['ordinal'])!=tr.ordinal or str(existing_tranche['base_sha'])!=str(plan.repo_base_sha) or existing_criteria != set(tr.criterion_ids): raise ValueError('conflicting admitted tranche')
   else:
    c.execute('INSERT INTO tranches(id,feature_id,ordinal,status,base_sha,integration_commands_json) VALUES (?,?,?, ?,?,"[]")',(tr.id,feature.id,tr.ordinal,'active' if tr.ordinal==0 else 'planned',plan.repo_base_sha))
   if not existing_tranche:
    for cid in tr.criterion_ids:c.execute('INSERT INTO tranche_criteria VALUES (?,?)',(tr.id,cid))
   if tr.ordinal:
    continue
   for t in tr.microtickets:
    q=t.contract(); c.execute('INSERT INTO tickets(id,feature_id,tranche_id,title,objective,criterion_ids_json,primary_symbol,allowed_files_json,new_test_files_json,forbidden_changes_json,patch_budget_json,verification_json,risk,review_required,max_attempts,dependencies_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(t.ticket_id,feature.id,tr.id,t.ticket_id,q['objective'],json.dumps(q['criterion_ids']),q['primary_symbol'],json.dumps(q['allowed_files']),json.dumps(q.get('new_test_files',[])),json.dumps(q['forbidden_changes']),json.dumps(q['patch_budget']),json.dumps(q['verification']),q['risk'],int(t.review_required),t.max_attempts,json.dumps(q['dependencies']),'draft',now,now)); active.append(t.ticket_id)
    for cid in t.criterion_ids:c.execute('INSERT INTO ticket_criteria VALUES (?,?)',(t.ticket_id,cid))
    ledger._inject_failure('after_generated_ticket')
    projection = generated_card_payload(feature, tr, t, repository_identity=str(repository_identity), repo_base_sha=str(repo_base_sha), repo_snapshot_hash=str(repo_snapshot_hash))
    event_id = ledger._append_event(c, entity_type='ticket', entity_id=t.ticket_id, event_type='generated_microticket_created', actor_id='controller', to_state='draft', payload={'feature_id': feature.id, 'tranche_id': tr.id, 'projection_key': projection['projection_key']})
    ledger._enqueue_generated_create_projection_in_transaction(c, ticket_id=t.ticket_id, event_id=event_id, payload=projection, idempotency_key=projection['projection_key'])
  if existing and existing['status'] == 'validated_pending_activation':
   c.execute("UPDATE decomposition_plans SET status='active', activated_at=? WHERE id=?", (now, existing['id']))
 return pid,tuple(active)
