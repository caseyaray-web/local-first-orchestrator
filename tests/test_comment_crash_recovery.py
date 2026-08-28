from __future__ import annotations
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.comment_delivery import CommentDeliveryCrash, CommentDeliveryPolicy, CommentDeliveryWorker, MarkerLookup
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState
class Clock:
 def __init__(self): self.now=10
 def __call__(self): return self.now
class Remote:
 writes_enabled=True
 def __init__(self): self.comments=[]; self.lookups=self.sends=0; self.crash_after_store=False
 def find_comment_marker(self,task,marker): self.lookups+=1; return MarkerLookup.FOUND if any(marker in x for x in self.comments) else MarkerLookup.NOT_FOUND
 def deliver_comment(self,task,payload,*,idempotency_key):
  self.sends+=1; self.comments.append(payload)
  if self.crash_after_store: raise CommentDeliveryCrash('remote died')
class CrashTests(unittest.TestCase):
 def setUp(self): self.t=TemporaryDirectory(); self.p=Path(self.t.name)/'x.db'; self.l=Ledger(self.p); self.l.migrate(); tid=self.l.create_ticket(title='t',state=CanonicalState.READY_LOCAL,external_id='e'); self.op=self.l.enqueue_evidence_comment(tid,1,'x'); self.c=Clock(); self.r=Remote()
 def tearDown(self): self.l.close(); self.t.cleanup()
 def worker(self,fault=None): return CommentDeliveryWorker(self.l,self.r,CommentDeliveryPolicy(lease_seconds=2,base_retry_delay=1),worker_id='w',clock=self.c,fault_injector=fault)
 def restart(self): self.l.close(); self.l=Ledger(self.p); self.l.migrate(); self.c.now=12
 def crash_then_reconcile(self,stage,remote_crash=False):
  self.r.crash_after_store=remote_crash
  fault=lambda s: (_ for _ in ()).throw(CommentDeliveryCrash(s)) if s==stage else None
  with self.assertRaises(CommentDeliveryCrash): self.worker(fault).deliver_one()
  existed=bool(self.r.comments); self.restart(); self.r.crash_after_store=False; result=self.worker().deliver_one(); self.assertEqual(result.status,'reconciled_delivered' if existed else 'delivered'); self.assertEqual(len(self.r.comments),1)
 def test_crash_after_not_found_recovers_and_sends_once(self): self.crash_then_reconcile('after_lookup_not_found')
 def test_remote_crash_after_store_reconciles(self): self.crash_then_reconcile('unused',True)
 def test_crash_after_adapter_success_reconciles(self): self.crash_then_reconcile('after_adapter_success')
 def test_crash_after_found_reconciles_again(self):
  self.r.comments.append(self.op['payload']); self.crash_then_reconcile('after_marker_found'); self.assertEqual(self.r.sends,0)
 def test_crash_after_local_delivery_is_terminal(self):
  with self.assertRaises(CommentDeliveryCrash): self.worker(lambda s: (_ for _ in ()).throw(CommentDeliveryCrash(s)) if s=='after_local_delivered' else None).deliver_one()
  self.restart(); self.assertEqual(self.worker().deliver_one().status,'no_work'); self.assertEqual(self.r.lookups,1)
if __name__=='__main__': unittest.main()
