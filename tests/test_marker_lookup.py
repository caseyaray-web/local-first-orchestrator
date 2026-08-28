from __future__ import annotations
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.comment_delivery import CommentDeliveryPolicy, CommentDeliveryWorker, MarkerLookup
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState

class Clock:
 def __init__(self,t=10): self.t=t
 def __call__(self): return self.t
class Adapter:
 writes_enabled=True
 def __init__(self,lookup): self.lookup=lookup; self.lookups=self.sends=0
 def find_comment_marker(self,*_): self.lookups+=1; return self.lookup() if callable(self.lookup) else self.lookup
 def deliver_comment(self,*_,**__): self.sends+=1
class LookupTests(unittest.TestCase):
 def scenario(self,lookup,max_attempts=3):
  temp=TemporaryDirectory(); path=Path(temp.name)/'l.db'; ledger=Ledger(path); ledger.migrate(); ticket=ledger.create_ticket(title='t',state=CanonicalState.READY_LOCAL,external_id='external'); op=ledger.enqueue_evidence_comment(ticket,1,'token=hidden'); clock=Clock(); adapter=Adapter(lookup); worker=CommentDeliveryWorker(ledger,adapter,CommentDeliveryPolicy(max_attempts=max_attempts,base_retry_delay=2),worker_id='w',clock=clock); return temp,path,ledger,op,clock,adapter,worker
 def test_found_reconciles_without_send(self):
  t,p,l,o,c,a,w=self.scenario(MarkerLookup.FOUND); self.assertEqual(w.deliver_one().status,'reconciled_delivered'); self.assertEqual((a.lookups,a.sends),(1,0)); t.cleanup()
 def test_not_found_sends_once(self):
  t,p,l,o,c,a,w=self.scenario(MarkerLookup.NOT_FOUND); self.assertEqual(w.deliver_one().status,'delivered'); self.assertEqual((a.lookups,a.sends),(1,1)); t.cleanup()
 def test_unavailable_defers_without_send(self):
  t,p,l,o,c,a,w=self.scenario(MarkerLookup.UNAVAILABLE); self.assertEqual(w.deliver_one().status,'reconciliation_deferred'); self.assertEqual(a.sends,0); self.assertEqual(l.comment_outbox(o['operation_id'])['status'],'retryable'); t.cleanup()
 def test_unsupported_fails_closed_without_send(self):
  t,p,l,o,c,a,w=self.scenario(MarkerLookup.UNSUPPORTED); self.assertEqual(w.deliver_one().status,'reconciliation_deferred'); self.assertEqual(a.sends,0); t.cleanup()
 def test_malformed_lookup_normalizes_to_unavailable(self):
  for value in ('bad',None,object()):
   t,p,l,o,c,a,w=self.scenario(value); self.assertEqual(w.deliver_one().status,'reconciliation_deferred'); self.assertEqual(a.sends,0); t.cleanup()
 def test_lookup_exception_normalizes_to_unavailable(self):
  t,p,l,o,c,a,w=self.scenario(lambda: (_ for _ in ()).throw(RuntimeError('token=secret'))); self.assertEqual(w.deliver_one().status,'reconciliation_deferred'); self.assertEqual(a.sends,0); self.assertNotIn('secret',l.comment_outbox(o['operation_id'])['last_error']); t.cleanup()
 def test_unavailable_reaches_attempt_limit(self): self._limit(MarkerLookup.UNAVAILABLE)
 def test_unsupported_reaches_attempt_limit(self): self._limit(MarkerLookup.UNSUPPORTED)
 def _limit(self,value):
  t,p,l,o,c,a,w=self.scenario(value,2); self.assertEqual(w.deliver_one().status,'reconciliation_deferred'); due=l.comment_outbox(o['operation_id'])['next_attempt_at']; c.t=due; self.assertEqual(w.deliver_one().status,'permanently_failed'); self.assertEqual(l.comment_outbox(o['operation_id'])['attempt_count'],2); self.assertEqual(w.deliver_one().status,'no_work'); self.assertEqual((a.lookups,a.sends),(2,0)); t.cleanup()
 def test_deferred_identity_survives_reopen(self):
  t,p,l,o,c,a,w=self.scenario(MarkerLookup.UNAVAILABLE); w.deliver_one(); before=l.comment_outbox(o['operation_id']); l.close(); l=Ledger(p); l.migrate(); after=l.comment_outbox(o['operation_id']); self.assertEqual(tuple(after[k] for k in ('operation_id','idempotency_key','external_task_id','payload')),tuple(before[k] for k in ('operation_id','idempotency_key','external_task_id','payload'))); self.assertIn(o['operation_id'],after['payload']); l.close(); t.cleanup()
if __name__=='__main__': unittest.main()
