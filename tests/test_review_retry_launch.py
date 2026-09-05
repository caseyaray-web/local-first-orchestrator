from __future__ import annotations
import json, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.ledger import Ledger
from local_first_orchestrator.states import CanonicalState

class ReviewRetryLaunchTests(unittest.TestCase):
 def setUp(self):
  self.t=TemporaryDirectory(); self.root=Path(self.t.name); self.l=Ledger(self.root/'x.db'); self.l.migrate(); self.ticket=self.l.create_ticket(title='x')
  for s in (CanonicalState.READY_LOCAL,CanonicalState.IMPLEMENTING,CanonicalState.VERIFYING,CanonicalState.LOCAL_REVIEW): self.l.transition(self.ticket,s)
  self.fp='a'*64; self.l.freeze_review_candidate(self.ticket,2,candidate_fingerprint=self.fp,validation_evidence='passed',implementation_invocation_id=None,runtime_identity={})
  self.l.start_model_invocation(invocation_id='failed',ticket_id=self.ticket,attempt_number=2,stage='review',provider='p',model='m',packet_hash='b'*64,worktree_path='/tmp/a',timeout_seconds=10); self.l.finish_model_invocation('failed',status='timeout',duration_seconds=1)
  self.auth=self.l.authorize_review_retry(self.ticket,operator_id='o',candidate_fingerprint=self.fp)
 def tearDown(self): self.l.close(); self.t.cleanup()
 def test_authorization_is_consumed_once_by_atomic_invocation_creation(self):
  one=self.l.launch_authorized_review(self.auth['authorization_id'],invocation_id='retry',provider='p',model='m',packet_hash='c'*64,worktree_path='/tmp/a',timeout_seconds=10)
  two=self.l.launch_authorized_review(self.auth['authorization_id'],invocation_id='other',provider='p',model='m',packet_hash='c'*64,worktree_path='/tmp/a',timeout_seconds=10)
  self.assertEqual(one['invocation_id'],two['invocation_id']); self.assertEqual(len(self.l.review_invocations(self.ticket,2)),2)
 def test_unconsumed_authorization_survives_restart_and_does_not_count(self):
  self.l.close(); self.l=Ledger(self.root/'x.db'); self.l.migrate()
  self.assertEqual(self.l.review_reconciliation_status(self.ticket)['review_invocation_count'],1)
  self.assertTrue(self.l.launch_authorized_review(self.auth['authorization_id'],invocation_id='retry',provider='p',model='m',packet_hash='c'*64,worktree_path='/tmp/a',timeout_seconds=10))
 def test_stale_started_is_preserved_as_terminal_and_not_live(self):
  self.l.launch_authorized_review(self.auth['authorization_id'],invocation_id='retry',provider='p',model='m',packet_hash='c'*64,worktree_path='/tmp/a',timeout_seconds=10)
  self.assertFalse(self.l.recover_stale_review_invocation('retry',now=100,stale_after_seconds=200))
  self.assertTrue(self.l.recover_stale_review_invocation('retry',now=10**10,stale_after_seconds=1))
  self.assertEqual(self.l.model_invocation('retry')['status'],'process_error')
if __name__=='__main__': unittest.main()
