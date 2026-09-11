from __future__ import annotations
import json,os,stat,subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.states import CanonicalState
_FAKE='''#!/usr/bin/env python3
import json,os,sys
log=os.environ['LOG']; state=os.environ['STATE']
args=sys.argv[1:]
with open(log,'a') as f:f.write(json.dumps(args)+'\\n')
def load():
 try:return json.load(open(state))
 except FileNotFoundError:return {'next_id':2,'tasks':{'1':{'id':'1','title':'x','body':'<!-- local-first-orchestrator -->','status':'scheduled'}},'keys':{}}
def save(x):json.dump(x,open(state,'w'))
if args[:3]!=['kanban','--board','board']:sys.exit(9)
a=args[3:]; data=load()
if a==['list','--json','--status','scheduled']:
 print(json.dumps([x for x in data['tasks'].values() if x['status']=='scheduled']))
elif len(a)==3 and a[0]=='show' and a[2]=='--json':
 task=data['tasks'].get(a[1]);
 if not task:sys.exit(3)
 print(json.dumps({'task':task}))
elif a and a[0]=='schedule':print('')
elif len(a)==11 and a[0]=='create' and a[2]=='--body' and a[4:]==['--workspace','scratch','--idempotency-key',a[7],'--initial-status','blocked','--json']:
 key=a[7]; task=data['tasks'].get(data['keys'].get(key,''))
 if not task:
  ident=str(data['next_id']);data['next_id']+=1;task={'id':ident,'title':a[1],'body':a[3],'status':'blocked','workspace_path':None,'idempotency_key':key};data['tasks'][ident]=task;data['keys'][key]=ident;save(data)
 print(json.dumps(task))
else:sys.exit(9)
'''
class ProcessReadTests(unittest.TestCase):
 def setUp(self):
  self.t=TemporaryDirectory();self.log=Path(self.t.name)/'log';self.state=Path(self.t.name)/'state';self.exe=Path(self.t.name)/'hermes';self.exe.write_text(_FAKE);self.exe.chmod(stat.S_IRWXU);self.old={k:os.environ.get(k) for k in ('LOG','STATE')};os.environ['LOG']=str(self.log);os.environ['STATE']=str(self.state)
 def tearDown(self):
  for k,v in self.old.items():os.environ.pop(k,None) if v is None else os.environ.__setitem__(k,v)
  self.t.cleanup()
 def adapter(self,**kw):return HermesBoardAdapter(executable=str(self.exe),board='board',**kw)
 def test_read_and_create_contract(self):
  a=self.adapter(allow_writes=True);self.assertEqual(len(a.import_candidates()),1);self.assertEqual(a.get_task('1').id,'1');self.assertEqual(a.create_microticket('new','body',idempotency_key='K'),'2');self.assertEqual(a.create_microticket('new','body',idempotency_key='K'),'2');calls=[json.loads(x) for x in self.log.read_text().splitlines()];self.assertIn(['kanban','--board','board','create','new','--body','body','--workspace','scratch','--idempotency-key','K','--initial-status','blocked','--json'],calls)
 def test_create_fails_closed(self):
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=lambda *_,**__:subprocess.CompletedProcess((),0,'{}',''))
  with self.assertRaises(RuntimeError):a.create_microticket('x','b',idempotency_key='K')
 def test_native_graph_read_and_link_contract(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   if argv[-1]=='--json':
    return subprocess.CompletedProcess(argv,0,json.dumps({'task':{'id':'child','title':'c','body':'','status':'todo','workspace_path':None},'parents':['parent-b','parent-a'],'children':['leaf']}),'')
   return subprocess.CompletedProcess(argv,0,'Linked parent-a -> child\n','')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  task=a.get_task('child')
  self.assertEqual(task.parents,('parent-a','parent-b'));self.assertEqual(task.children,('leaf',))
  a.link_dependency('parent-a','child')
  self.assertIn([str(self.exe),'kanban','--board','board','link','parent-a','child'],calls)
if __name__=='__main__':unittest.main()
