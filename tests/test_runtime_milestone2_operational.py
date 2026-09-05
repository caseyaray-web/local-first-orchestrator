from __future__ import annotations
import json, subprocess, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.controller import InjectedCrash, LocalFirstController, RuntimeConfig
from local_first_orchestrator.hermes_board import ExternalTicket
from local_first_orchestrator.ledger import Ledger

class Board:
    is_fake=False
    def __init__(self): self.states=[]; self.comments=[]
    def set_state(self, ticket_id, state, *, idempotency_key): self.states.append((ticket_id,state.value,idempotency_key))
    def add_comment(self, ticket_id, comment): self.comments.append((ticket_id,comment))

class Model:
    def __init__(self, actions, reviews=None): self.actions=list(actions); self.reviews=list(reviews or []); self.calls=[]
    def invoke(self,purpose,packet,*,artifact_dir,workdir=None):
        self.calls.append((purpose,packet))
        if purpose=='implementation': self.actions.pop(0)(Path(workdir))
        payload=self.reviews.pop(0) if purpose=='review' and self.reviews else ({'verdict':'pass','criterion_results':[{'criterion_id':'AC-1','status':'pass','evidence':'ok'}],'findings':[],'suggestions':[]} if purpose=='review' else {})
        return type('R',(),{'payload':payload,'artifact_path':Path(artifact_dir)/f'{purpose}-result.json'})()

def contract(**changes):
    c={'objective':'make value ok','criterion_ids':['AC-1'],'primary_symbol':'app.py::value','allowed_files':['app.py'],'forbidden_changes':['no api'],'patch_budget':{'max_files':1,'max_changed_lines':10},'verification':{'commands':[['python','-c',"from app import value; assert value() == 'ok'"]]},'risk':'low','review_required':True,'max_attempts':2}; c.update(changes); return c

class OperationalMilestone2(unittest.TestCase):
    def setUp(self):
        self.t=TemporaryDirectory(); self.r=Path(self.t.name)/'repo'; self.r.mkdir(); self.g('init','-b','main'); self.g('config','user.email','x@y'); self.g('config','user.name','x'); (self.r/'app.py').write_text("def value():\n return 'bad'\n"); self.g('add','.'); self.g('commit','-m','base'); self.l=Ledger(Path(self.t.name)/'l.db'); self.l.migrate(); self.b=Board(); self.cfg=RuntimeConfig(self.r,Path(self.t.name)/'wt',Path(self.t.name)/'art',(self.r,))
    def tearDown(self): self.l.close(); self.t.cleanup()
    def g(self,*a): return subprocess.run(('git',*a),cwd=self.r,text=True,capture_output=True,check=True)
    def card(self,**kw): return ExternalTicket('x','x','<!-- local-first-orchestrator -->\n```local-first-contract\n'+json.dumps(contract(**kw))+'\n```','scheduled',str(self.r))
    def make_controller(self,model,crash=None):
        tid=LocalFirstController(self.l,self.b,self.cfg,local_model=model).import_card(self.card()); ctl=LocalFirstController(self.l,self.b,self.cfg,local_model=model,fault_injector=(lambda s: (_ for _ in ()).throw(InjectedCrash(s)) if s==crash else None)); return ctl,tid
    def good(self,p): (p/'app.py').write_text("def value():\n return 'ok'\n")
    def bad(self,p): (p/'app.py').write_text("def value():\n return 'bad'\n")
    def test_review_failure_repairs_same_ticket_with_bounded_finding(self):
        finding={'verdict':'repair','criterion_results':[{'criterion_id':'AC-1','status':'fail','evidence':'wrong'}],'findings':[{'severity':'blocking','criterion_id':'AC-1','file':'app.py','symbol':'app.py::value','evidence':'wrong result','minimal_repair':'return ok','verification':'run check','fingerprint_input':'wrong result'}],'suggestions':['out of scope']}
        m=Model([self.good,self.good],[finding,{'verdict':'pass','criterion_results':[{'criterion_id':'AC-1','status':'pass','evidence':'ok'}],'findings':[],'suggestions':[]}]); ctl,tid=self.make_controller(m); ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual(self.l.get_ticket(tid)['state'],'done'); self.assertEqual([x[0] for x in m.calls],['implementation','review','implementation','review']); self.assertEqual(self.l.attempt_count(tid),2); self.assertEqual(self.l.connection.execute('select count(*) from tickets').fetchone()[0],1)
    def test_attempt_exhaustion_no_third_implementation(self):
        m=Model([self.bad,self.bad]); ctl,tid=self.make_controller(m); ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual([x[0] for x in m.calls],['implementation','implementation']); self.assertEqual(self.l.get_ticket(tid)['state'],'needs_triage'); self.assertEqual(self.l.connection.execute('select count(*) from board_projection_outbox').fetchone()[0],1); ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual(len(m.calls),2)
    def test_crash_after_implementation_replays_validation(self):
        m=Model([self.good]); ctl,tid=self.make_controller(m,'implementation_completed');
        with self.assertRaises(InjectedCrash): ctl.execute(tid,repository=self.r,allow_board_writes=True)
        ctl.fault_injector=None; ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual([x[0] for x in m.calls],['implementation','review'])
    def test_crash_after_commit_does_not_commit_twice(self):
        m=Model([self.good]); ctl,tid=self.make_controller(m,'accepted_commit_created');
        with self.assertRaises(InjectedCrash): ctl.execute(tid,repository=self.r,allow_board_writes=True)
        ctl.fault_injector=None; ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual(len(m.calls),2); self.assertEqual(self.l.connection.execute("select count(*) from accepted_evidence").fetchone()[0],1)
    def test_out_of_scope_finding_is_nonblocking(self):
        m=Model([self.good],[{'verdict':'repair','criterion_results':[{'criterion_id':'AC-1','status':'pass','evidence':'ok'}],'findings':[{'severity':'blocking','criterion_id':'OTHER','file':'evil.py','symbol':'x','evidence':'x','minimal_repair':'x','verification':'x','fingerprint_input':'x'}],'suggestions':[]}]); ctl,tid=self.make_controller(m); ctl.execute(tid,repository=self.r,allow_board_writes=True); self.assertEqual(self.l.get_ticket(tid)['state'],'done')

    def test_malformed_local_review_preserves_frozen_candidate_for_reconciliation(self):
        m=Model([self.good],[{'verdict':'pass','criterion_results':[],'findings':[],'suggestions':[]}])
        ctl,tid=self.make_controller(m)
        with self.assertRaisesRegex(ValueError,'every criterion'):
            ctl.execute(tid,repository=self.r,allow_board_writes=True)
        self.assertEqual(self.l.get_ticket(tid)['state'],'local_review')
        self.assertEqual(self.l.review_candidate(tid)['status'],'review_infrastructure_failed')
        self.assertEqual(self.l.review_reconciliation_status(tid)['classification'],'review_invocation_failed_no_verdict')

if __name__=='__main__': unittest.main()
