from __future__ import annotations
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.comment_delivery import CommentDeliveryWorker, CommentDeliveryPolicy
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
class Remote:
 writes_enabled=True
 def __init__(self): self.comments={}; self.lookups=0; self.sends=0; self.ambiguous=False
 def find_comment_marker(self, task, marker): self.lookups+=1; return 'found' if any(marker in comment for comment in self.comments.get(task,[])) else 'not_found'
 def deliver_comment(self, task, comment, *, idempotency_key):
  self.sends+=1; self.comments.setdefault(task,[]).append(comment)
  if self.ambiguous: raise RuntimeError('ambiguous accepted')
class ReconcileTests(unittest.TestCase):
 def setUp(self): self.t=TemporaryDirectory(); self.l=Ledger(Path(self.t.name)/'x.db'); self.l.migrate(); self.tid=self.l.create_ticket(title='x',state=CanonicalState.READY_LOCAL); self.op=self.l.enqueue_evidence_comment(self.tid,1,'x'); self.r=Remote(); self.w=CommentDeliveryWorker(self.l,self.r,CommentDeliveryPolicy(base_retry_delay=0),worker_id='w',clock=lambda:10)
 def tearDown(self): self.l.close(); self.t.cleanup()
 def test_existing_marker_reconciles_without_send(self):
  self.r.comments.setdefault(self.op['external_task_id'],[]).append(self.op['payload']); result=self.w.deliver_one(); self.assertEqual(result.status,'reconciled_delivered'); self.assertEqual(self.r.sends,0)
 def test_ambiguous_acceptance_retries_then_reconciles(self):
  self.r.ambiguous=True; self.assertEqual(self.w.deliver_one().status,'reconciliation_deferred'); self.r.ambiguous=False; self.assertEqual(self.w.deliver_one().status,'reconciled_delivered'); self.assertEqual(self.r.sends,1)
if __name__=='__main__': unittest.main()
