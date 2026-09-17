from __future__ import annotations
import json,os,shutil,stat,subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from local_first_orchestrator.hermes_board import HermesBoardAdapter
from local_first_orchestrator.comment_delivery import MarkerLookup
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
elif len(a)==11 and a[0]=='create' and a[2]=='--body' and a[4:]==['--workspace','worktree','--idempotency-key',a[7],'--initial-status','blocked','--json']:
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
  a=self.adapter(allow_writes=True);self.assertEqual(len(a.import_candidates()),1);self.assertEqual(a.get_task('1').id,'1');self.assertEqual(a.create_microticket('new','body',idempotency_key='K'),'2');self.assertEqual(a.create_microticket('new','body',idempotency_key='K'),'2');calls=[json.loads(x) for x in self.log.read_text().splitlines()];self.assertIn(['kanban','--board','board','create','new','--body','body','--workspace','worktree','--idempotency-key','K','--initial-status','blocked','--json'],calls)
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
 def test_reclaim_for_repair_is_idempotent_once_dispatchable(self):
  calls=[]; remote={'status':'done'}
  def runner(argv,**kwargs):
   calls.append(list(argv))
   action=argv[4]
   if action=='show':
    return subprocess.CompletedProcess(argv,0,json.dumps({'task':{'id':'1','title':'x','body':'','status':remote['status'],'workspace_path':'/repo','workspace_kind':'worktree','assignee':'worker-code-local'},'parents':[],'children':[],'comments':[]}), '')
   if action=='reclaim':
    remote['status']='ready'; return subprocess.CompletedProcess(argv,0,'','')
   raise AssertionError(argv)
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  self.assertEqual(a.reclaim_for_repair('1',reason='fix parity').status,'ready')
  first_reclaims=sum(1 for call in calls if call[4]=='reclaim')
  self.assertEqual(first_reclaims,1)
  self.assertEqual(a.reclaim_for_repair('1',reason='fix parity').status,'ready')
  self.assertEqual(sum(1 for call in calls if call[4]=='reclaim'),1)
 def test_create_repair_task_inserts_dependency_and_parks_downstream_first(self):
  calls=[]
  tasks={
   'old':{'id':'old','title':'TK-2','body':'body','status':'done','workspace_path':'/repo/.worktrees/old','assignee':'worker-code-local','parents':['p'],'children':['child']},
   'child':{'id':'child','title':'TK-3','body':'body','status':'ready','workspace_path':'/repo/.worktrees/child','parents':['old'],'children':[]},
  }
  def payload(task_id):
   row=tasks[task_id]
   return {'task':{k:v for k,v in row.items() if k not in {'parents','children'}},'parents':list(row['parents']),'children':list(row['children']),'comments':[]}
  def runner(argv,**kwargs):
   calls.append(list(argv)); action=argv[4]
   if action=='show': return subprocess.CompletedProcess(argv,0,json.dumps(payload(argv[5])), '')
   if action=='create':
    if 'repair' not in tasks:
     tasks['repair']={'id':'repair','title':argv[5],'body':argv[argv.index('--body')+1],'status':'blocked','workspace_path':'/repo/.worktrees/old','assignee':'worker-code-local','parents':['old'],'children':[]}; tasks['old']['children'].append('repair')
    return subprocess.CompletedProcess(argv,0,json.dumps({'id':'repair'}),'')
   if action=='block': tasks[argv[5]]['status']='blocked'; return subprocess.CompletedProcess(argv,0,'','')
   if action=='link':
    parent,child=argv[5],argv[6]
    if parent not in tasks[child]['parents']: tasks[child]['parents'].append(parent)
    if child not in tasks[parent]['children']: tasks[parent]['children'].append(child)
    return subprocess.CompletedProcess(argv,0,'','')
   if action=='unlink':
    parent,child=argv[5],argv[6]
    if parent in tasks[child]['parents']: tasks[child]['parents'].remove(parent)
    if child in tasks[parent]['children']: tasks[parent]['children'].remove(child)
    return subprocess.CompletedProcess(argv,0,'','')
   if action=='unblock': tasks[argv[5]]['status']='ready'; return subprocess.CompletedProcess(argv,0,'','')
   raise AssertionError(argv)
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner,implementation_profile='worker-code-local')
  task=a.create_repair_task('old',title='TK-2 repair attempt 2',body='repair body',workspace_path='/repo/.worktrees/old',downstream_child_ids=('child',),idempotency_key='repair-key',reason='fix parity')
  self.assertEqual(task.status,'ready'); self.assertEqual(tasks['child']['status'],'blocked')
  self.assertEqual(tasks['child']['parents'],['repair']); self.assertEqual(tasks['old']['children'],['repair']); self.assertEqual(tasks['repair']['children'],['child'])
  actions=[call[4] for call in calls if call[4] in {'block','link','unlink','unblock'}]
  self.assertLess(actions.index('block'),actions.index('unlink')); self.assertEqual(actions[-1],'unblock')
 def test_guarded_dispatch_spawns_only_authorized_task(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   if '--dry-run' in argv:
    return subprocess.CompletedProcess(argv,0,json.dumps({'spawned':[{'task_id':'repair','assignee':'worker-code-local','workspace':'/repo'}]}),'')
   return subprocess.CompletedProcess(argv,0,json.dumps({'spawned':[{'task_id':'repair','assignee':'worker-code-local','workspace':'/repo'}]}),'')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  self.assertEqual(a.dispatch_one_if_allowed({'repair'}),'repair')
  self.assertEqual(len(calls),2)
  self.assertIn('--dry-run',calls[0]); self.assertNotIn('--dry-run',calls[1])

 def test_guarded_dispatch_refuses_plan_with_unrelated_task(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   return subprocess.CompletedProcess(argv,0,json.dumps({'spawned':[{'task_id':'repair','assignee':'worker','workspace':'/repo'},{'task_id':'unrelated','assignee':'worker','workspace':'/other'}]}),'')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  with self.assertRaisesRegex(RuntimeError,'non-Local-First'):
   a.dispatch_one_if_allowed({'repair'})
  self.assertEqual(len(calls),1)
  self.assertIn('--dry-run',calls[0])

 def test_comment_marker_lookup_uses_show_json_comments(self):
  def runner(argv,**kwargs):
   payload={'task':{'id':'1','title':'x','body':'','status':'scheduled','workspace_path':None},'comments':[{'author':'local-first-orchestrator','body':'hello <!-- local-first-comment:abc -->','created_at':1}], 'parents':[], 'children':[]}
   return subprocess.CompletedProcess(argv,0,json.dumps(payload),'')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',runner=runner)
  self.assertEqual(a.find_comment_marker('1','<!-- local-first-comment:abc -->'),MarkerLookup.FOUND)
  self.assertEqual(a.find_comment_marker('1','<!-- local-first-comment:missing -->'),MarkerLookup.NOT_FOUND)
 def test_execution_snapshot_parses_runs_from_one_show_payload(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   payload={
    'task':{'id':'1','title':'x','body':'','status':'scheduled','workspace_path':'/repo','session_id':'s1','branch_name':'worker/x','started_at':10,'completed_at':20},
    'parents':['p1'],'children':['c1'],'comments':[], 'latest_summary':{'future':{'value':3}}, 'future_root':{'opaque':['x',2]},
    'runs':[{'id':7,'status':'completed','outcome':'completed','started_at':10,'ended_at':20,'summary':'worker completed','profile':'worker-code','worker_pid':123,'metadata':{'source':'dispatcher'}}],
   }
   return subprocess.CompletedProcess(argv,0,json.dumps(payload),'')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',runner=runner)
  snap=a.execution_snapshot('1')
  self.assertEqual(snap.task.id,'1'); self.assertEqual(snap.task.parents,('p1',)); self.assertEqual(snap.task.children,('c1',))
  self.assertEqual(snap.session_id,'s1'); self.assertEqual(snap.branch_name,'worker/x')
  self.assertEqual(snap.runs[0].id,7); self.assertEqual(snap.runs[0].profile,'worker-code'); self.assertEqual(snap.runs[0].metadata,{'source':'dispatcher'})
  self.assertEqual(snap.raw_snapshot['latest_summary'], {'future': {'value': 3}})
  self.assertEqual(snap.raw_snapshot['future_root'], {'opaque': ['x', 2]})
  self.assertEqual(calls,[[str(self.exe),'kanban','--board','board','show','1','--json']])
 def test_nonterminal_state_projection_is_idempotent_when_already_scheduled(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   if argv[-1]=='--json':
    return subprocess.CompletedProcess(argv,0,json.dumps({'task':{'id':'1','title':'x','body':'','status':'scheduled','workspace_path':None},'parents':[],'children':[],'comments':[]}), '')
   return subprocess.CompletedProcess(argv,0,'','')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  a.set_state('1',CanonicalState.LOCAL_REVIEW,idempotency_key='K')
  self.assertEqual(calls,[[str(self.exe),'kanban','--board','board','show','1','--json']])
 def test_done_projection_promotes_scheduled_task_before_complete(self):
  calls=[]
  def runner(argv,**kwargs):
   calls.append(list(argv))
   if argv[-1]=='--json':
    return subprocess.CompletedProcess(argv,0,json.dumps({'task':{'id':'1','title':'x','body':'','status':'scheduled','workspace_path':None},'parents':[],'children':[],'comments':[]}), '')
   return subprocess.CompletedProcess(argv,0,'','')
  a=HermesBoardAdapter(executable=str(self.exe),board='board',allow_writes=True,runner=runner)
  a.set_state('1',CanonicalState.DONE,idempotency_key='K')
  self.assertEqual(calls,[
   [str(self.exe),'kanban','--board','board','show','1','--json'],
   [str(self.exe),'kanban','--board','board','unblock','1','--reason','local-first completion K'],
   [str(self.exe),'kanban','--board','board','complete','1','--result','local-first projection K'],
  ])
 def test_locked_reader_is_byte_canonical_with_complete_real_cli_history(self):
  from hermes_cli.sqlite_util import open_db
  from tests.hermes_board_fixture import initialize_board
  from local_first_orchestrator.native_release_approval import canonical_snapshot_json
  board_path=Path(self.t.name)/'kanban'/'boards'/'board'/'kanban.db'
  board_path.parent.mkdir(parents=True)
  initialize_board(board_path, task_id='T', status='blocked')
  with open_db(board_path, db_label='kanban:board', busy_timeout_ms=5000, wal=False, check_same_thread=False) as conn:
   for i in range(61):
    conn.execute('INSERT INTO task_events (task_id,kind,payload,created_at,run_id) VALUES (?,?,?,?,?)', ('T',f'event-{i}',json.dumps({'i':i}),i+2, None))
   for i in range(2):
    conn.execute('INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)', ('T','author',f'comment-{i}',i+1))
   conn.execute('DELETE FROM task_runs WHERE task_id=?', ('T',))
   conn.execute('INSERT INTO task_runs(task_id,profile,step_key,status,claim_lock,claim_expires,worker_pid,max_runtime_seconds,last_heartbeat_at,started_at,ended_at,outcome,summary,metadata,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('T','worker-code-local',None,'completed',None,None,None,None,None,50,60,'completed','previous',None,None))
   conn.execute('INSERT INTO task_runs(task_id,profile,step_key,status,claim_lock,claim_expires,worker_pid,max_runtime_seconds,last_heartbeat_at,started_at,ended_at,outcome,summary,metadata,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('T','worker-code-local',None,'running','lock',99,1234,None,None,100,None,None,None,None,None))
   conn.execute('UPDATE tasks SET status=?,session_id=?,started_at=? WHERE id=?', ('blocked','session-T',100,'T'))
   conn.execute('INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,workspace_path,branch_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('P','parent','','impl','done',1,'test',1,'worktree','/tmp/p','main'))
   conn.execute('INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,workspace_kind,workspace_path,branch_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('C','child','','impl','todo',1,'test',1,'worktree','/tmp/c','main'))
   conn.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', ('P','T'))
   conn.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', ('T','C'))
   conn.commit()
  executable=shutil.which('hermes') or '/home/ocadmin/.local/bin/hermes'
  env={**os.environ,'HERMES_HOME':self.t.name}
  cli=subprocess.run((executable,'kanban','--board','board','show','T','--json'),env=env,text=True,capture_output=True,check=True)
  actual=json.loads(cli.stdout)
  self.assertNotIn('current_run_id',actual['task'])
  self.assertEqual(actual['task']['status'],'blocked')
  self.assertEqual([r['status'] for r in actual['runs']],['completed','running'])
  self.assertTrue(actual['comments'])
  self.assertTrue(actual['parents'])
  self.assertTrue(actual['children'])
  self.assertEqual(actual['latest_summary'],'previous')
  adapter=HermesBoardAdapter(executable=executable,board='board',board_db_path=board_path)
  with open_db(board_path, db_label='kanban:board', busy_timeout_ms=5000, wal=False, check_same_thread=False) as conn:
   locked=adapter._snapshot_from_connection(conn,'T').raw_snapshot
  self.assertEqual(canonical_snapshot_json(actual),canonical_snapshot_json(locked))

if __name__=='__main__':unittest.main()
