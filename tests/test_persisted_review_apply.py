from __future__ import annotations
import json, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
class PersistedReviewApplyTests(unittest.TestCase):
 def setUp(self):
  self.d=TemporaryDirectory(); self.r=Path(self.d.name); self.l=Ledger(self.r/'x.db'); self.l.migrate(); self.t=self.l.create_ticket(title='x',contract={'objective':'x','criterion_ids':['A'],'primary_symbol':'x.py::x','allowed_files':['x.py'],'forbidden_changes':['x'],'patch_budget':{'max_files':1,'max_changed_lines':1},'verification':{'commands':[['true']]},'risk':'low','max_attempts':2})
  for s in (CanonicalState.READY_LOCAL,CanonicalState.IMPLEMENTING,CanonicalState.VERIFYING,CanonicalState.LOCAL_REVIEW): self.l.transition(self.t,s)
  self.fp='a'*64; self.l.freeze_review_candidate(self.t,2,candidate_fingerprint=self.fp,validation_evidence='passed',implementation_invocation_id=None,runtime_identity={})
  self.a=self.r/'review.json'; self.a.write_text(json.dumps({'payload':{'verdict':'pass','criterion_results':[{'criterion_id':'A','status':'pass','evidence':'ok'}],'findings':[],'suggestions':[]}})); self.l.record_model_stage(self.t,2,'review',purpose='review',adapter='x',request_hash='b'*64,response_artifact=str(self.a),worktree_path='/tmp/x',base_sha='c'*40,diff_hash=self.fp)
 def tearDown(self): self.l.close(); self.d.cleanup()
 def test_stage_pending_applies_once_without_model(self):
  self.assertEqual(self.l.review_reconciliation_status(self.t)['classification'],'valid_review_stage_pending_application')
  one=self.l.apply_persisted_review(self.t,2); two=self.l.apply_persisted_review(self.t,2)
  self.assertEqual(one['verdict'],two['verdict']); self.assertEqual(self.l.review_reconciliation_status(self.t)['classification'],'valid_review_applied'); self.assertEqual(self.l.connection.execute('select count(*) from review_results').fetchone()[0],1)
  def test_pass_is_persisted_without_acceptance_or_disposition(self):
   self.assertEqual(self.l.apply_persisted_review(self.t,2)['status'],'applied_only')
   self.assertEqual(self.l.get_ticket(self.t)['state'],CanonicalState.LOCAL_REVIEW.value)
   self.assertEqual(self.l.connection.execute('select verdict from review_results').fetchone()[0],'pass')
  def test_non_passing_result_is_persisted_without_repair(self):
   self.a.write_text(json.dumps({'payload':{'verdict':'escalate','criterion_results':[],'findings':[],'suggestions':[]}}))
   self.assertEqual(self.l.apply_persisted_review(self.t,2)['verdict'],'escalate')
   self.assertEqual(self.l.get_ticket(self.t)['state'],CanonicalState.LOCAL_REVIEW.value)
   self.assertEqual(self.l.connection.execute('select count(*) from events where to_state=?',(CanonicalState.REPAIRING.value,)).fetchone()[0],0)
  def test_generic_needs_triage_cannot_bridge(self):
   self.l.transition(self.t,CanonicalState.NEEDS_TRIAGE)
   with self.assertRaises(PermissionError): self.l.apply_persisted_review(self.t,2)
   self.assertEqual(self.l.connection.execute('select count(*) from review_results').fetchone()[0],0)
  def test_historical_bridge_is_atomic_on_failure(self):
   original=self.l._append_event
   def fail(*args,**kwargs):
    if kwargs.get('event_type') == 'review_recorded': raise RuntimeError('injected')
    return original(*args,**kwargs)
   self.l._append_event=fail
   with self.assertRaises(RuntimeError): self.l.apply_persisted_review(self.t,2)
   self.assertEqual(self.l.get_ticket(self.t)['state'],CanonicalState.LOCAL_REVIEW.value)
   self.assertEqual(self.l.connection.execute('select count(*) from review_results').fetchone()[0],0)
if __name__=='__main__': unittest.main()
