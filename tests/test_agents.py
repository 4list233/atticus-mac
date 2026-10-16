"""Pure Python: fake Claude/MCP, scratch folders, no Mac actions or connector writes."""
import contextlib
import io
import json
import os
import shlex
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

import atticus

WORKER = '''import json,sys,time,os
pending=False
for line in sys.stdin:
 e=json.loads(line)
 if e.get('type')=='control_request':
  data={'mcpServers':[{'name':'fixture','status':'connected','tools':[{'name':'inspect','annotations':{'readOnly':True}},{'name':'change','annotations':{'destructive':True}}]}]} if e['request']['subtype']=='mcp_status' else {}
  print(json.dumps({'type':'control_response','response':{'request_id':e['request_id'],'subtype':'success','response':data}}),flush=True)
 elif e.get('type')=='user':
  text=e['message']['content']
  print(json.dumps({'type':'system','subtype':'init','session_id':'11111111-1111-4111-8111-111111111111'}),flush=True)
  print(json.dumps({'type':'user','message':{'role':'user','content':text}}),flush=True)
  if text.startswith('RATE'):
   if not pending: pending=True;continue
   print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'allowed'}}),flush=True)
   print(json.dumps({'type':'result','subtype':'success','queued_turn_count':0,'is_error':False,'total_cost_usd':.001,'result':'[agent-result {"summary":"Finished after steering.","changes":"","links":[]}]'}),flush=True)
   continue
  print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'[agent-plan {"steps":["Read the brief","Verify the result"],"completed":1}]'},{'type':'text','text':'Reading the task'},{'type':'tool_use','id':'t','name':'mcp__fixture__inspect','input':{}}]}}),flush=True)
  print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'t','content':[{'type':'text','text':'Links: [{"title":"Source material","url":"https://fixture.example/source"}]'}]}]}}),flush=True)
  if text.startswith('WAIT'): continue
  print(json.dumps({'type':'result','session_id':'11111111-1111-4111-8111-111111111111','is_error':text.startswith('FAIL'),'total_cost_usd':.001,'usage':{'input_tokens':3},'result':'[agent-result '+json.dumps({'summary':'Finished the task.','changes':'Created a result.','links':[]})+']'}),flush=True)
'''


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(); self.addCleanup(self.root.cleanup)
        self.prefs = {'safe': True, 'agentUserTools': False}
        self.patch = patch.multiple(atticus, DIR=self.root.name, HOME=self.root.name, PREFS=str(Path(self.root.name)/'settings.json'),
                                    pref=lambda k,d:self.prefs.get(k,d))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.events = []
        worker = Path(self.root.name)/'worker.py'; worker.write_text(WORKER)
        self.spawned = []
        def launch(row):
            p = subprocess.Popen([sys.executable,str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True,cwd=row['directory'])
            p.ask=row['directory']; self.spawned.append((row['id'],p)); return p
        self.manager = atticus.Agents(emit=lambda e:self.events.append(json.loads(json.dumps(e))),launch=launch)
        self.addCleanup(self.manager.close)

    def until(self, predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('Timed out waiting for fake agent')

    def prepare(self, brief='WAIT fixture', **extra):
        return self.manager.prepare({'title':'Fixture task','brief':brief,'context':'Known text','connectors':[],**extra})

    def gate(self, name, inp=None, directory=None, answer='n', **env):
        stream=io.StringIO()
        with patch.dict(os.environ,{'ATTICUS_ASK':directory or self.root.name,**env}),patch('sys.stdin',io.StringIO(json.dumps({'tool_name':name,'tool_input':inp or {}}))), \
             patch('sys.stdout',stream),patch.object(atticus,'wait_commit',return_value=True),patch.object(atticus,'ask_user',return_value=answer) as ask:
            atticus.gate()
        lines=stream.getvalue().splitlines()
        return json.loads(lines[-1])['hookSpecificOutput'] if lines else {},ask

    def test_parse_handoff_is_exact_first_output_and_bounded(self):
        marker='[agent '+json.dumps({'title':'Fill the document','brief':'Complete the requested fields','context':'Known URL','connectors':['mcp__fixture__inspect','bad name'],'resume':'abc','destination':'meeting'})+']'
        row=atticus.parse_handoff(marker)
        self.assertEqual(row['title'],'Fill the document');self.assertEqual(row['connectors'],['mcp__fixture__inspect'])
        self.assertEqual(row['destination'],'meeting')
        self.assertEqual(atticus.parse_handoff(marker+'\nMore words')['title'],row['title'])
        self.assertEqual(atticus.parse_handoff('[agent {"title":"One two three four five","brief":"Task"}]')['title'],'One two three four')
        for bad in ('hello\n'+marker,marker+' More words','[agent {}]','[agent {"title":3,"brief":"x"}]','[agent broken]'):
            self.assertIsNone(atticus.parse_handoff(bad),bad)

    def test_full_installed_prompt_including_handoff_stays_under_budget(self):
        self.prefs.update(pointer=True,askMe=True,learnMe=True,tips=True,learn=True)
        prompt=atticus.system_prompt(atticus.ASK_FIRST)
        installed='/Users/saarthranka/Applications/Atticus.app/Contents/Resources/atticus.py'
        self.assertLessEqual(len(prompt.replace(atticus.ME,installed)),10000)
        self.assertLess(prompt.index(atticus.AGENT_HANDOFF),250);self.assertEqual(prompt.count(atticus.AGENT_HANDOFF),1)

    def test_sole_live_agent_is_steered_without_a_new_agent_question(self):
        self.assertIn('If exactly one is live, use it',atticus.AGENT_HANDOFF)
        self.assertIn('never ask which or start another',atticus.AGENT_HANDOFF)
        live=self.manager.new();live.update(status='running',title='Live task',brief='Keep working',created=1);self.manager.save(live)
        for i in range(12):
            row=self.manager.new();row.update(status='done',title='Old task',brief='Done',created=i+2);self.manager.save(row)
        self.assertIn(live['id'],atticus.agent_context())   # recent history cannot crowd out the steering target

    def test_matching_old_task_is_context_for_a_new_brief_not_its_result(self):
        brief='WAIT compare the best rated items under the requested budget and create a new report'
        old=self.manager.new();old.update(status='done',title='Earlier comparison',brief=brief,
                                        summary='Earlier findings',links=['https://fixture.example/old-report'])
        self.manager.save(old)
        row=self.prepare(brief,context=atticus.agent_context())
        self.assertNotEqual(row['id'],old['id']);self.assertNotIn('result',row)
        self.assertIn('A matching old result does not complete a new brief',atticus.AGENT_SYSTEM)
        self.manager.start(row['id'])
        self.until(lambda:any(e.get('event')=='claude' and e['data'].get('type')=='user'
                              for e in atticus.read_rows(str(Path(row['directory'])/'log.jsonl'))))
        sent=next(e['text'] for e in atticus.read_rows(str(Path(row['directory'])/'log.jsonl')) if e.get('event')=='followup')
        self.assertTrue(sent.startswith(brief));self.assertIn(old['id'],sent);self.assertIn('https://fixture.example/old-report',sent)
        self.assertEqual(row['status'],'running')

    def test_agent_correction_is_memory_evidence_and_survives_a_generated_resume(self):
        with patch.object(atticus,'ME_FILE',str(Path(self.root.name)/'me.json')),patch.object(atticus,'RECENT',str(Path(self.root.name)/'recent.jsonl')):
            row=self.prepare(request='Original user words');self.manager.start(row['id'])
            with patch.dict(os.environ,{'ATTICUS_ASK':row['directory']}):
                self.assertEqual(atticus.profile_add('people','old@example.test','contact email',source='you'),'saved')
                fact=atticus.me()['facts'][0];words='WAIT correction: contact email: corrected@example.test'
                send=self.manager.send
                def dispatch(agent,text):
                    self.assertEqual(atticus.profile_set(fact['id'],'contact email: corrected@example.test'),'saved')
                    send(agent,text)
                with patch.object(self.manager,'send',side_effect=dispatch):
                    self.manager.steer({'steer':row['id'],'brief':words,'context':'Earlier:\nA: invented@example.test\nRequest: '+words})
            self.assertIn('corrected@example.test',atticus.profile_context('Email contact'))
            self.manager.command({'action':'stop','id':row['id']})
            self.manager.start(row['id'],'WAIT generated resume: invented@example.test')
            with patch.dict(os.environ,{'ATTICUS_ASK':row['directory']}):
                self.assertTrue(atticus._request_contains('corrected@example.test'))
                self.assertFalse(atticus._request_contains('invented@example.test'))

    def test_move_offer_uses_elapsed_work_or_three_calls_without_pausing(self):
        p=unittest.mock.Mock();p.ask=self.root.name;p.waiting=threading.Event()
        move=atticus.MainMove('Request: Continue the task',p);self.addCleanup(move.close)
        move.last=0
        with patch.object(atticus.time,'monotonic',return_value=14),contextlib.redirect_stdout(io.StringIO()) as output:
            move.poll();self.assertFalse(move.offered)
            p.waiting.set();move.last=0;move.poll();self.assertEqual(move.elapsed,14)
            p.waiting.clear()
            for i in range(3):move.record({'type':'assistant','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{'file_path':'fixture'}}]}})
            move.poll();self.assertTrue(move.offered);self.assertFalse(move.moved);self.assertIn('⇢ ',output.getvalue())
        other=atticus.MainMove('Request: Work',p);self.addCleanup(other.close);other.last=0
        with patch.object(atticus.time,'monotonic',return_value=15),contextlib.redirect_stdout(io.StringIO()):other.poll()
        self.assertTrue(other.offered)

    def test_accepted_move_stops_foreground_and_preserves_completed_and_uncertain_work(self):
        worker=Path(self.root.name)/'moving.py'
        worker.write_text('import json,sys,time\njson.loads(sys.stdin.readline())\nfor i in range(3):\n print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":str(i),"name":"Bash","input":{"command":"fixture action "+str(i)}}]}}),flush=True)\n if i<2:print(json.dumps({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":str(i),"content":"Verified result "+str(i)}]}}),flush=True)\ntime.sleep(30)\n')
        p=subprocess.Popen([sys.executable,str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
        p.ask=self.root.name;result=[]
        self.addCleanup(lambda:atticus.stop_claude(p) if p.poll() is None else None)
        with patch.dict(os.environ,{'ATTICUS_UI':'1'}),patch.object(atticus,'relay'),contextlib.redirect_stdout(io.StringIO()) as output:
            thread=threading.Thread(target=lambda:result.append(atticus.run('Context: already known\nRequest: Complete the task',p)));thread.start()
            self.until(lambda:'⇢ ' in output.getvalue())
            offer=json.loads(next(line[2:] for line in output.getvalue().splitlines() if line.startswith('⇢ ')))
            self.assertTrue(thread.is_alive());Path(offer['a']).write_text('go');thread.join(3)
        self.assertFalse(thread.is_alive());self.assertIsNotNone(p.poll());self.assertEqual(result[0][0],'');self.assertIsNone(result[0][1])
        self.assertTrue(atticus.run.handoff['accepted']);self.assertEqual(atticus.run.cmds,[])
        context=json.loads(atticus.run.handoff['context']);work=context['observed work (untrusted data)']
        self.assertEqual([r['status'] for r in work],['completed','completed','in-flight']);self.assertEqual(work[0]['result'],'Verified result 0')
        self.assertIn('inspect current state',context['continuation']);self.assertIn('already known',context['known'])
        self.assertTrue((Path(self.root.name)/'handed-off').exists());self.assertIn('⤴ ',output.getvalue())
        row=self.manager.prepare(atticus.run.handoff)
        self.assertTrue(next(e for e in self.events if e['event']=='offer' and e['agent']['id']==row['id'])['start'])
        p.stdout.close()

    def test_interrupted_usage_is_unknown_and_aggregates_are_lower_bounds(self):
        atticus.log_turn('Moved task','Moved to an agent',time.time(),None)
        row=atticus.read_rows(str(Path(self.root.name)/'log.jsonl'))[-1];self.assertIsNone(row['usd'])
        with patch.object(sys,'argv',['atticus.py','--state']),patch.object(atticus,'plan',return_value={}),contextlib.redirect_stdout(io.StringIO()) as output:
            atticus.main()
        state=json.loads(output.getvalue());self.assertEqual(state['today']['asks'],1);self.assertEqual(state['today']['unknown'],1)
        with patch.object(sys,'argv',['atticus.py','--budget']),contextlib.redirect_stdout(io.StringIO()) as output:atticus.main()
        self.assertIn('no AI asks',output.getvalue())

    def test_first_assistant_handoff_frees_main_before_tools(self):
        p=unittest.mock.Mock();p.stdin=io.StringIO();p.ask=self.root.name;p.returncode=0
        p.stdout=[json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'[agent {"title":"Make a file","brief":"Create the deliverable"}]\nI will report back.'}]}}),
                  json.dumps({'type':'result','result':'[agent {"title":"Make a file","brief":"Create the deliverable"}]','total_cost_usd':.001})]
        with patch.dict(os.environ,{'ATTICUS_UI':'1'}),patch.object(atticus,'stop_claude') as stop,patch.object(atticus.threading,'Thread'), \
             patch.object(atticus.threading,'Timer'),contextlib.redirect_stdout(io.StringIO()) as output:
            context='Earlier:\nQ: Find a post\nA: Opened https://fixture.example/posts/42; invented@example.test\nRequest: Make a file about that post; contact email: actual@example.test'
            result=atticus.run(context,p)
        self.assertEqual(result,('',.001,[]));self.assertTrue(atticus.run.ok);stop.assert_not_called()
        self.assertIn('⤴ ',output.getvalue());self.assertEqual(atticus.run.handoff['context'],context)
        row=self.manager.prepare(atticus.run.handoff);self.manager.start(row['id'])
        sent=next(e['text'] for e in atticus.read_rows(str(Path(row['directory'])/'log.jsonl')) if e.get('event')=='followup')
        self.assertIn(context,sent)   # reference evidence survives the main-to-agent route unchanged
        with patch.dict(os.environ,{'ATTICUS_ASK':row['directory']}),patch.object(atticus,'RECENT',str(Path(self.root.name)/'recent.jsonl')):
            self.assertTrue(atticus._request_contains('actual@example.test'))
            self.assertFalse(atticus._request_contains('invented@example.test'))
        decision,_=self.gate('Bash',{'command':'echo no'})
        self.assertEqual(decision['permissionDecision'],'deny')

    def test_normal_flags_remain_strict_and_agent_persists(self):
        with patch.object(atticus.shutil,'which',return_value='/fixture/claude'),patch.object(atticus.subprocess,'Popen') as popen,patch.object(atticus.atexit,'register'):
            atticus.start_claude()
            args=popen.call_args.args[0];self.assertIn('--strict-mcp-config',args);self.assertIn('--no-session-persistence',args)
            row=self.manager.new();row.update(session='saved-session',connectors=['mcp__fixture__inspect'])
            atticus.start_claude('opus',row)
            args=popen.call_args.args[0];options=popen.call_args.kwargs
        system=args[args.index('--system-prompt')+1]
        self.assertIn(atticus.AGENT_SYSTEM,system);self.assertNotIn(atticus.AGENT_HANDOFF,system)
        self.assertIn('Never invent URLs/parameters',system)
        self.assertNotIn('--strict-mcp-config',args);self.assertNotIn('--no-session-persistence',args)
        self.assertEqual(args[args.index('--resume')+1],'saved-session');self.assertEqual(options['cwd'],row['directory'])
        self.assertNotIn('ANTHROPIC_API_KEY',options['env']);self.assertEqual(options['env']['CLAUDE_CODE_DISABLE_CLAUDE_MDS'],'1')
        settings=json.loads(args[args.index('--settings')+1]);self.assertTrue(settings['sandbox']['enabled'])
        self.assertEqual(settings['hooks']['PreToolUse'][0]['matcher'],'.*');self.assertNotIn('disableAllHooks',settings)
        self.assertIn('Write,Edit',args[args.index('--tools')+1]);self.assertIn('mcp__fixture__inspect',args[args.index('--tools')+1])
        self.assertEqual(args[args.index('--permission-mode')+1],'dontAsk')

    def test_agent_model_uses_its_own_router_model_and_explicit_credentials(self):
        for model in ('haiku','sonnet','opus'):
            self.prefs['agentModel']=model
            self.assertEqual(atticus.agent_model(),model)
        self.prefs.update(apiBase='https://fixture.invalid/anthropic',apiModel='main-model',apiKey='fixture-key')
        self.assertEqual(atticus.agent_model(),'main-model')
        self.prefs['agentApiModel']='agent-model'
        row=self.manager.new()
        with patch.object(atticus.shutil,'which',return_value='/fixture/claude'),patch.object(atticus.subprocess,'Popen') as launch, \
             patch.object(atticus.atexit,'register'),patch.dict(os.environ,{'ANTHROPIC_AUTH_TOKEN':'inherited-fixture'}):
            process=atticus.start_claude(atticus.agent_model(),row);process.errors.close()
            args=launch.call_args.args[0];env=launch.call_args.kwargs['env']
            self.assertEqual(args[args.index('--model')+1],'agent-model')
            self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'],'fixture-key')
            self.assertEqual(env['ANTHROPIC_BASE_URL'],self.prefs['apiBase'])
            self.assertEqual({env[f'ANTHROPIC_DEFAULT_{m}_MODEL'] for m in ('HAIKU','SONNET','OPUS')},{'agent-model'})
            process=atticus.start_claude();process.errors.close()
            self.assertEqual(launch.call_args.kwargs['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'],'main-model')
        self.prefs.pop('apiBase')
        self.assertEqual(atticus.agent_model(),'opus')

    def test_changing_router_agent_model_replaces_only_the_idle_worker(self):
        self.prefs.update(apiBase='https://fixture.invalid',apiModel='main-model',agentApiModel='first-model')
        running=self.prepare();self.manager.start(running['id']);idle=self.manager.idle
        self.until(lambda:idle['id'] in self.manager.processes)
        old=self.manager.processes[idle['id']]
        self.prefs['agentApiModel']='next-model'
        offered=self.prepare('WAIT another task')
        self.assertIsNotNone(old.poll())
        self.assertEqual(offered['_model'],'next-model')
        self.assertEqual(running['_model'],'first-model')
        self.assertIsNone(self.manager.processes[running['id']].poll())

    def test_warm_offer_sends_no_user_message_and_not_now_kills_group(self):
        row=self.prepare();p=self.manager.processes[row['id']]
        self.assertEqual(row['status'],'offered');self.assertNotIn(row['id'],self.manager.outstanding)
        self.manager.command({'action':'cancel','id':row['id']});p.wait(timeout=2)
        self.assertEqual(row['status'],'stopped');self.assertNotIn(row['id'],self.manager.processes)

    def test_unanswered_offer_expires_without_removing_the_start_choice(self):
        row=self.prepare();p=self.manager.processes[row['id']];row['_warmAt']=time.monotonic()-601
        self.manager.tick();self.assertIsNotNone(p.poll());self.assertEqual(row['status'],'offered')
        self.manager.start(row['id']);self.assertEqual(row['status'],'running');self.assertIn(row['id'],self.manager.processes)

    def test_three_agent_cap_queue_and_stop_advances_fourth(self):
        rows=[]
        for _ in range(4):
            row=self.prepare();self.manager.start(row['id']);rows.append(row)
        self.assertEqual(self.manager.running(),3);self.assertEqual(rows[-1]['status'],'queued')
        self.manager.command({'action':'stop','id':rows[0]['id']})
        self.assertEqual(rows[-1]['status'],'running');self.assertEqual(self.manager.running(),3)

    def test_one_idle_warm_process_reused_and_expires(self):
        row=self.prepare();self.manager.start(row['id']);idle=self.manager.idle
        self.assertIsNotNone(idle)
        self.assertIs(self.prepare(),idle);self.assertIsNone(self.manager.idle)
        self.manager.start(idle['id']);unused=self.manager.idle;p=self.manager.processes[unused['id']]
        self.manager.idle_at=time.monotonic()-601;self.manager.tick();p.wait(timeout=2)
        self.assertIsNone(self.manager.idle);self.assertNotIn(unused['id'],self.manager.processes)

    def test_prewarmed_process_is_never_persisted_as_idle_history(self):
        row=self.prepare();self.manager.start(row['id']);idle=self.manager.idle
        self.until(lambda:idle['id'] in self.manager.processes)
        self.assertFalse((Path(idle['directory'])/'state.json').exists())
        atticus.private_json(str(Path(idle['directory'])/'state.json'),idle)   # stale state written by 1.2
        self.assertNotIn(idle['id'],[r['id'] for r in atticus.agent_history()])

    def test_result_log_private_resume_and_main_context(self):
        row=self.prepare('DONE fixture');self.manager.start(row['id'])
        self.until(lambda:row['status']=='done')
        self.assertEqual(row['plan'],['Read the brief','Verify the result']);self.assertEqual(row['completed'],1)
        self.assertEqual(row['summary'],'Finished the task.');self.assertIn(row['id'],atticus.agent_context())
        self.assertTrue(row['session']);self.assertEqual(row['cost'],.001)
        self.assertEqual(row['sources'][0]['title'],'Source material');self.assertEqual(row['sources'][0]['domain'],'fixture.example')
        self.assertTrue(any(e.get('sources') for e in row['timeline']))
        path=Path(row['directory'])/'log.jsonl';self.assertEqual(path.stat().st_mode&0o777,0o600)
        events=atticus.read_rows(str(path));self.assertTrue(any(e.get('event')=='claude' for e in events))
        self.assertEqual((Path(row['directory'])/'state.json').stat().st_mode&0o777,0o600)
        self.manager.start(row['id'],'WAIT followup');self.assertEqual(row['status'],'running');self.assertTrue(row['session'])
        self.manager.command({'action':'seen','id':row['id']});self.assertTrue(row['seen']);self.assertIn('seenAt',row)

    def test_steer_line_reaches_running_agent_stdin_and_resumes_ended_one(self):
        words='For that AC filter agent, can you steer it? I meant 12 x 24, not 24 x 24?'
        steer=atticus.parse_handoff('[steer {"id":"abc","text":"12×24, not 24×24"}]')
        self.assertEqual(steer['steer'],'abc');self.assertEqual(steer['ack'],'Got it: 12×24, not 24×24')
        self.assertIsNone(atticus.parse_handoff('[steer {"id":"abc"}]'))
        row=self.prepare();self.manager.start(row['id']);p=self.manager.processes[row['id']];offers=len([e for e in self.events if e.get('event')=='offer'])
        self.manager.prepare({**steer,'steer':row['id'],'context':words})
        self.assertIs(self.manager.processes[row['id']],p);self.assertEqual(self.manager.outstanding[row['id']],2)   # same process, no new agent
        self.assertEqual(len([e for e in self.events if e.get('event')=='offer']),offers)
        self.assertEqual(row['timeline'][-1],{'user':words});self.assertEqual(self.events[-1]['event'],'steered')
        self.assertEqual(self.events[-1]['agent']['steer']['ack'],'Got it: 12×24, not 24×24')
        done=self.prepare('DONE fixture');self.manager.start(done['id']);self.until(lambda:done['status']=='done')
        self.manager.prepare({'steer':done['id'],'brief':'WAIT also check Home Depot','title':'','ack':'Got it'})
        self.assertEqual(done['status'],'running');self.assertTrue(done['session'])
        self.manager.prepare({'steer':'gone','brief':'x','title':''});self.assertEqual(self.events[-1],{'event':'steered','missing':True})

    def test_steered_result_after_rate_limit_finishes_and_pings_once(self):
        row=self.prepare('RATE original');self.manager.start(row['id'])
        self.manager.command({'action':'box','busy':False,'visible':True})
        self.manager.prepare({'steer':row['id'],'brief':'RATE only products under $200','title':'','ack':'Got it'})
        self.until(lambda:any(e.get('event')=='done' for e in self.events))
        self.assertEqual(row['summary'],'Finished after steering.')
        self.until(lambda:row['id'] not in self.manager.outstanding)
        self.assertEqual([e['event'] for e in self.events].count('done'),1)
        self.assertEqual([e['event'] for e in self.events].count('ping'),1)
        self.manager.pump_ping()
        self.assertEqual([e['event'] for e in self.events].count('ping'),1)
        logged=[e.get('data',{}).get('type') for e in atticus.read_rows(str(Path(row['directory'])/'log.jsonl')) if e.get('event')=='claude']
        self.assertEqual(logged[-2:],['rate_limit_event','result'])

    def test_followup_running_uses_existing_stdin(self):
        row=self.prepare();self.manager.start(row['id']);p=self.manager.processes[row['id']]
        self.manager.command({'action':'followup','id':row['id'],'text':'WAIT another instruction'})
        self.assertIs(self.manager.processes[row['id']],p);self.assertEqual(self.manager.outstanding[row['id']],2)
        self.assertEqual(Path(row['directory'],'request').read_text(),'WAIT another instruction')

    def test_superseded_offer_stops_warm_process_and_cancelled_followup_keeps_task(self):
        first=self.prepare();p=self.manager.processes[first['id']]
        second=self.prepare();self.assertEqual(first['status'],'stopped');self.assertIsNotNone(p.poll())
        self.manager.start(second['id']);p=self.manager.processes[second['id']]
        self.prepare('New instruction',resume=second['id'])
        self.manager.command({'action':'cancel','id':second['id']})
        self.assertEqual(second['status'],'running');self.assertIs(self.manager.processes[second['id']],p)

    def test_completed_followup_warms_resume_before_start_and_not_now_keeps_result(self):
        row=self.prepare('DONE fixture');self.manager.start(row['id']);self.until(lambda:row['status']=='done')
        self.prepare('Next instruction',resume=row['id']);p=self.manager.processes[row['id']]
        self.assertNotIn(row['id'],self.manager.outstanding)
        self.manager.command({'action':'cancel','id':row['id']})
        self.assertIsNotNone(p.poll());self.assertEqual(row['status'],'done');self.assertEqual(row['summary'],'Finished the task.')

    def test_quiet_notification_dispatch_has_no_sound_or_interpolated_script(self):
        with patch.object(sys,'argv',['atticus.py','--agent-notify','A "ready" result']),patch.object(atticus,'osa') as osa:
            atticus.main()
        script,text=osa.call_args.args
        self.assertIn('display notification',script);self.assertNotIn('sound',script);self.assertEqual(text,'A "ready" result')

    def test_fake_mcp_metadata_read_write_and_unknown_split(self):
        class MCP(BaseHTTPRequestHandler):
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert request['method']=='tools/list'
                response={'jsonrpc':'2.0','id':request['id'],'result':{'tools':[{'name':'inspect','annotations':{'readOnlyHint':True}},{'name':'mutate','annotations':{'readOnlyHint':False,'destructiveHint':True}}]}}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(response).encode())
            def log_message(self,*_):pass
        server=HTTPServer(('127.0.0.1',0),MCP);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        import urllib.request
        request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}',data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:tools=json.load(response)['result']['tools']
        row=self.prepare();self.manager.metadata(row,{'mcpServers':[{'name':'fixture','status':'connected','tools':tools}]})
        decision,ask=self.gate('mcp__fixture__inspect',directory=row['directory'])
        self.assertEqual(decision['permissionDecision'],'allow');ask.assert_not_called();self.assertNotIn('updatedInput',decision)
        for tool in ('mcp__fixture__mutate','mcp__fixture__missing'):
            decision,ask=self.gate(tool,directory=row['directory'],answer='n');self.assertEqual(decision['permissionDecision'],'deny');ask.assert_called_once()
        decision,_=self.gate('mcp__fixture__mutate',directory=row['directory'],answer='task');self.assertEqual(decision['permissionDecision'],'allow')
        decision,ask=self.gate('mcp__fixture__missing',directory=row['directory']);self.assertEqual(decision['permissionDecision'],'allow');ask.assert_not_called()

    def test_prep_never_writes_even_with_safe_mode_off(self):
        self.prefs['safe']=False
        decision,ask=self.gate('mcp__fixture__mutate',ATTICUS_AGENT_PREP='1',answer='y')
        self.assertEqual(decision['permissionDecision'],'deny');ask.assert_not_called()

    def test_agent_bash_write_edit_need_approval_without_bypassing_hard_rules(self):
        for name,inp in [('Bash',{'command':'python3 -c "print(1)"'}),('Write',{'file_path':'result.csv','content':'x'}),('Edit',{'file_path':'result.csv'})]:
            decision,ask=self.gate(name,inp,ATTICUS_AGENT='fixture',answer='n')
            self.assertEqual(decision['permissionDecision'],'deny');self.assertTrue(ask.call_args.args[0]['task'])
        decision,_=self.gate('Bash',{'command':'atticus.py --wait'},ATTICUS_AGENT='fixture',answer='y')
        self.assertEqual(decision['permissionDecision'],'deny')

    def test_trusted_read_helpers_pass_and_shell_mutations_still_ask(self):
        read='python3 '+atticus.ME+' --window'
        self.assertTrue(atticus.agent_read_command(read));self.assertFalse(atticus.agent_read_command(read+'; echo x > result'))
        self.assertFalse(atticus.agent_read_command('python3 '+atticus.ME+' --hands type Answer value'))
        with patch.object(atticus,'preview_commands'):
            decision,ask=self.gate('Bash',{'command':read},ATTICUS_AGENT='fixture')
        ask.assert_not_called();self.assertNotEqual(decision.get('permissionDecision'),'deny')
        self.assertTrue(decision['updatedInput']['dangerouslyDisableSandbox'])

    def test_helper_identity_survives_python_cd_symlinks_and_spaces(self):
        helper=Path(self.root.name)/'App Resources'/'atticus.py';helper.parent.mkdir();helper.write_text('fixture')
        alias=Path(self.root.name)/'helper';alias.symlink_to(helper)
        with patch.object(atticus,'ME',str(helper)),patch.object(atticus,'preview_commands'):
            for command in (shlex.join([str(helper),'--read','https://fixture.example/?a=1&b=2']),
                            shlex.join(['python3',str(alias),'--read','https://fixture.example/']),
                            'cd '+shlex.quote(str(helper.parent))+' && python3 atticus.py --read https://fixture.example/'):
                self.assertTrue(atticus.agent_read_command(command),command)
                decision,ask=self.gate('Bash',{'command':command},ATTICUS_AGENT='fixture')
                ask.assert_not_called();self.assertTrue(decision['updatedInput']['dangerouslyDisableSandbox'])
                self.assertEqual(shlex.split(decision['updatedInput']['command'])[:2],[sys.executable,str(helper)])
            self.assertTrue(atticus.agent_read_command('python3 atticus.py --read https://fixture.example/',str(helper.parent)))
            other=Path(self.root.name)/'atticus.py';other.write_text('different copy')
            for command in (str(other)+' --read https://fixture.example/',
                            str(alias)+' --read https://fixture.example/ && touch changed',
                            str(alias)+' --read https://fixture.example/ > changed',
                            str(alias)+' --hands type Field value',
                            'python3 -c '+shlex.quote(str(helper)+' --read https://fixture.example/')):
                self.assertFalse(atticus.agent_read_command(command),command)

    def test_sources_render_only_observed_http_links_with_titles(self):
        data=[{'url':'https://fixture.example/page','title':'Page title'},
              {'text':'[Second page](https://other.example/path)\nhttps://plain.example/path'},
              {'url':'javascript:alert(1)'},{'url':'https://user:secret@private.example/'}, {'url':'https://['}]
        sources=atticus.agent_sources(data)
        self.assertEqual([s['domain'] for s in sources],['fixture.example','other.example','plain.example'])
        self.assertEqual(sources[0]['title'],'Page title');self.assertEqual(sources[1]['title'],'Second page')
        self.assertEqual(len(atticus.agent_sources([{'url':'https://fixture.example/'+str(i)} for i in range(100)])),12)
        self.assertEqual(len(atticus.agent_sources([{'url':'https://fixture.example/'+str(i)} for i in range(100)],100)),100)
        self.assertEqual(atticus.agent_sources('['*1500+'0'+']'*1500),[])

    def test_managed_agent_urls_require_user_or_tool_provenance(self):
        identifier='a'*32;directory=Path(self.root.name)/'agents'/identifier;directory.mkdir(parents=True)
        atticus.agent_url_observe(str(directory),[
            'User URL: https://shop.example/items?category=vacuum&max=300&sort=rating',
            {'url':'https://results.example/product/42?ref=search'}])
        env={'ATTICUS_AGENT':identifier,'ATTICUS_ASK':str(directory)}
        with patch.dict(os.environ,env):
            self.assertTrue(atticus.agent_url_allowed('https://shop.example/items?category=vacuum&max=300'))
            self.assertTrue(atticus.agent_url_allowed('https://results.example/product/42?ref=search'))
            for invented in ('https://shop.example/items?category=vacuum&max=300&sort=rating&page=2',
                             'https://shop.example/other','https://unseen.example/'):
                self.assertFalse(atticus.agent_url_allowed(invented),invented)
            with patch.object(atticus.subprocess,'run') as run:
                self.assertIn('not supplied',atticus.web('read','https://unseen.example/'))
                self.assertIn('not supplied',atticus.show('https://unseen.example/'))
                run.assert_not_called()
            with patch.object(sys,'argv',['atticus.py','--read','https://unseen.example/']),contextlib.redirect_stdout(io.StringIO()) as output:
                atticus.main()
            self.assertIn('not supplied',output.getvalue())
        denied,ask=self.gate('WebFetch',{'url':'https://unseen.example/'},directory=str(directory),ATTICUS_AGENT=identifier)
        self.assertEqual(denied['permissionDecision'],'deny');self.assertIn('not supplied',denied['permissionDecisionReason']);ask.assert_not_called()
        allowed,ask=self.gate('WebFetch',{'url':'https://results.example/product/42'},directory=str(directory),ATTICUS_AGENT=identifier)
        self.assertEqual(allowed['permissionDecision'],'allow');ask.assert_not_called()

    def test_agent_prompt_and_tool_results_seed_url_provenance(self):
        row=self.prepare('WAIT supplied https://user.example/page?keep=1&drop=2');self.manager.start(row['id'])
        self.until(lambda:row.get('sources'))
        with patch.dict(os.environ,{'ATTICUS_AGENT':row['id'],'ATTICUS_ASK':row['directory']}):
            self.assertTrue(atticus.agent_url_allowed('https://user.example/page?keep=1'))
            self.assertTrue(atticus.agent_url_allowed('https://fixture.example/source'))
            self.assertFalse(atticus.agent_url_allowed('https://fixture.example/invented'))

    def test_http_redirects_and_page_links_become_observed(self):
        class Pages(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/start':
                    self.send_response(302);self.send_header('Location','/final?session=1');self.end_headers();return
                self.send_response(200);self.end_headers()
                self.wfile.write(b'<a href="/next?item=1&tracking=2">Next</a>')
            def log_message(self,*_):pass
        server=HTTPServer(('127.0.0.1',0),Pages);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        identifier='b'*32;directory=Path(self.root.name)/'agents'/identifier;directory.mkdir(parents=True)
        start=f'http://127.0.0.1:{server.server_port}/start'
        atticus.agent_url_observe(str(directory),start)
        with patch.dict(os.environ,{'ATTICUS_AGENT':identifier,'ATTICUS_ASK':str(directory)}):
            self.assertIn('Next',atticus.get(start))
            self.assertTrue(atticus.agent_url_allowed(f'http://127.0.0.1:{server.server_port}/final'))
            self.assertTrue(atticus.agent_url_allowed(f'http://127.0.0.1:{server.server_port}/next?item=1'))

    def test_made_things_come_from_fresh_tool_results_files_and_reported_links(self):
        now=time.time();iso=lambda t:time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime(t))
        # The real Drive create_file shape: a JSON text result with id, mimeType, title and viewUrl.
        doc={'id':'123ZOJhbMVJAuUwnLBTrox-crQ_hisX7jbi3i-4FaD4g','mimeType':'application/vnd.google-apps.document','title':'E-bike comparison','createdTime':iso(now),'viewUrl':'https://docs.google.com/document/d/123ZOJhbMVJAuUwnLBTrox-crQ_hisX7jbi3i-4FaD4g/edit?usp=drivesdk'}
        sheet={'id':'1sheetsheetsheetsheet','mimeType':'application/vnd.google-apps.spreadsheet','name':'Budget','createdTime':iso(now)}
        event={'id':'evt12345678','summary':'Review','created':iso(now),'htmlLink':'https://calendar.example/event?eid=1'}
        old={'id':'1oldoldoldoldold','mimeType':'application/vnd.google-apps.document','title':'Found, not made','createdTime':iso(now-86400)}
        made=atticus.agent_made([json.dumps(doc),[{'type':'text','text':json.dumps({'files':[sheet,old]})}],{'event':event}],now-5)
        self.assertEqual([m['title'] for m in made],['E-bike comparison','Budget','Review'])
        self.assertEqual(made[0]['url'],doc['viewUrl']);self.assertEqual(made[1]['url'],'https://docs.google.com/spreadsheets/d/1sheetsheetsheetsheet/edit')
        self.assertEqual(atticus.agent_made('['*1500+'0'+']'*1500,now),[])
        folder=Path(self.root.name)/'agent';folder.mkdir();(folder/'table.csv').write_text('a,b');outside=Path(self.root.name)/'agent-other.csv';outside.write_text('x')
        row={'directory':str(folder),'made':made[:1],'wrote':[str(folder/'table.csv'),str(outside)],
             'links':['https://docs.google.com/document/d/123ZOJhbMVJAuUwnLBTrox-crQ_hisX7jbi3i-4FaD4g/edit','https://source.example/a','https://slack.example/archives/C1/p1','table.csv','../agent-other.csv','missing.md'],
             'timeline':[{'sources':[{'url':'https://source.example/a?ref=1','title':'Source','domain':'source.example'}]}]}
        self.assertEqual([m['url'] for m in atticus.agent_results(row)],[doc['viewUrl'],str((folder/'table.csv').resolve()),'https://slack.example/archives/C1/p1'])

    def test_task_only_plugins_do_not_copy_hooks_or_claude_instructions(self):
        home=Path(self.root.name);source=home/'.claude';source.mkdir()
        (source/'skills').mkdir();(source/'CLAUDE.md').write_text('private instructions');(source/'settings.json').write_text(json.dumps({'hooks':{'PreToolUse':['bad']},'mcpServers':{'fixture':{'command':'fixture'}},'enabledPlugins':{'fixture@market':True}}))
        plugin=home/'plugin';(plugin/'.claude-plugin').mkdir(parents=True);(plugin/'skills').mkdir();(plugin/'hooks').mkdir()
        (plugin/'.claude-plugin'/'plugin.json').write_text('{"name":"fixture","hooks":"hooks/hooks.json"}')
        (plugin/'.mcp.json').write_text('{"mcpServers":{"plugin":{"command":"${CLAUDE_PLUGIN_ROOT}/worker"}}}')
        (source/'plugins').mkdir();(source/'plugins'/'installed_plugins.json').write_text(json.dumps({'plugins':{'fixture@market':[{'scope':'user','installPath':str(plugin)}]}}))
        row=self.manager.new();flags=atticus.user_agent_tools(row['directory']);self.assertIn('--plugin-dir',flags)
        root=Path(flags[flags.index('--plugin-dir')+1]);self.assertTrue((root/'0'/'skills').is_dir());self.assertTrue((root/'1'/'skills').is_dir())
        self.assertFalse((root/'0'/'CLAUDE.md').exists());self.assertFalse((root/'1'/'hooks').exists())
        metadata=json.loads((root/'1'/'.claude-plugin'/'plugin.json').read_text());self.assertNotIn('hooks',metadata)
        self.assertNotIn('--mcp-config',flags)
        config=json.loads((Path(atticus.agent_gate_dir(row['directory']))/'user-mcp.json').read_text());self.assertEqual(config['plugin_fixture_plugin']['command'],str(plugin)+'/worker')

    def test_skill_frontmatter_cannot_install_hooks_or_yaml_merges(self):
        source=Path(self.root.name)/'skills'/'fixture';source.mkdir(parents=True)
        (source/'SKILL.md').write_text('---\nname: fixture\ndescription: Make a useful file\nhooks:\n  PreToolUse:\n    - command: forbidden\n<<: *hooks\n---\nKeep these instructions.\n')
        target=Path(self.root.name)/'sanitized';atticus.copy_agent_skills(str(source),str(target))
        text=(target/'SKILL.md').read_text();self.assertNotIn('hooks:',text);self.assertNotIn('<<:',text)
        self.assertIn('description: "Make a useful file"',text);self.assertIn('Keep these instructions.',text)

    def test_done_ping_waits_for_box_and_typing_pause(self):
        row=self.prepare('DONE fixture');self.manager.start(row['id']);self.until(lambda:row['status']=='done')
        self.assertFalse(any(e.get('event')=='ping' for e in self.events))
        self.manager.command({'action':'box','busy':True,'visible':True});self.assertFalse(any(e.get('event')=='ping' for e in self.events))
        self.manager.command({'action':'box','busy':False,'typing':True,'visible':True});self.assertFalse(any(e.get('event')=='ping' for e in self.events))
        self.manager.command({'action':'box','busy':False,'visible':False,'lastKey':time.time()});self.assertFalse(any(e.get('event')=='ping' for e in self.events))
        self.manager.command({'action':'box','busy':False,'visible':False,'lastKey':time.time()-2})
        self.assertEqual(sum(e.get('event')=='ping' for e in self.events),1)
        self.assertFalse(atticus.agent_ping_ready({'quiet':True,'visible':True}));self.assertFalse(atticus.agent_ping_ready({'listening':True,'visible':True}))

    def test_metadata_is_invalidated_when_process_restarts(self):
        row=self.manager.new();self.manager.metadata(row,{'mcpServers':[{'name':'fixture','status':'connected','tools':[{'name':'inspect','annotations':{'readOnly':True}}]}]})
        self.assertTrue(atticus.connector_annotations({'tool_name':'mcp__fixture__inspect'},row['directory']))
        self.manager.warm(row);self.assertEqual(atticus.connector_annotations({'tool_name':'mcp__fixture__inspect'},row['directory']),{})

    def test_failed_retry_and_clear_keep_running_tasks(self):
        row=self.prepare('FAIL fixture');self.manager.start(row['id']);self.until(lambda:row['status']=='failed')
        running=self.prepare();self.manager.start(running['id'])
        deliverable=Path(row['directory'])/'result.csv';deliverable.write_text('Verified result')
        self.manager.command({'action':'clear'});self.assertIn(row['id'],self.manager.rows)
        self.manager.command({'action':'clear-failed'});self.assertNotIn(row['id'],self.manager.rows);self.assertIn(running['id'],self.manager.rows)
        self.assertEqual(deliverable.read_text(),'Verified result');self.assertNotIn(row['id'],atticus.agent_context())

    def test_restart_resumes_accepted_sessions_with_cap_and_renewed_approval(self):
        rows=[self.prepare('WAIT task '+str(i)) for i in range(4)]
        for row in rows:self.manager.start(row['id'])
        self.until(lambda:all(row.get('session') for row in rows[:3]))
        offered=self.prepare('WAIT not accepted')
        first=rows[0];first['status']='needs-you';first['question']={'q':'Old approval'};self.manager.save(first)
        session=first.pop('session');self.manager.save(first)
        approval=Path(atticus.agent_gate_dir(first['directory']))/'allow-task';approval.write_text('true')
        self.manager.close()
        restored=atticus.Agents(emit=lambda _:None,launch=self.manager.launch);self.addCleanup(restored.close)
        self.assertEqual(restored.rows[offered['id']]['status'],'stopped')
        restored.tick();self.assertEqual(restored.running(),3)
        self.assertEqual(restored.rows[rows[3]['id']]['status'],'queued')
        self.until(lambda:bool([e for e in atticus.read_rows(str(Path(first['directory'])/'log.jsonl')) if e.get('event')=='followup' and 'Atticus restarted' in e.get('text','')]))
        self.assertIsNone(restored.rows[first['id']]['question'])
        self.assertEqual(restored.rows[first['id']]['session'],session);self.assertFalse(approval.exists())
        with patch.object(atticus.shutil,'which',return_value='/fixture/claude'),patch.object(atticus.subprocess,'Popen') as launch:
            process=atticus.start_claude('sonnet',restored.rows[first['id']]);args=launch.call_args.args[0];process.errors.close()
            self.assertEqual(args[args.index('--resume')+1],session)
        self.assertIn('Inspect current state',next(e['text'] for e in reversed(atticus.read_rows(str(Path(first['directory'])/'log.jsonl'))) if e.get('event')=='followup'))

    def test_remove_terminal_rows_keeps_files_and_rejects_running(self):
        running=self.prepare();self.manager.start(running['id'])
        self.manager.command({'action':'remove','id':running['id']});self.assertIn(running['id'],self.manager.rows)
        for status in ('done','failed','stopped'):
            row=self.manager.new();row.update(status=status,title='Old task',brief='Old task');self.manager.rows[row['id']]=row;self.manager.save(row)
            file=Path(row['directory'])/'result.csv';file.write_text('kept')
            self.manager.command({'action':'remove','id':row['id']})
            self.assertNotIn(row['id'],self.manager.rows);self.assertEqual(file.read_text(),'kept')
        self.assertTrue(all(r['id']==running['id'] for r in atticus.agent_history()))

    def test_meeting_result_uses_existing_gather_without_second_connector_ask(self):
        with patch.object(atticus,'meeting_connector_call',side_effect=AssertionError('No second connector ask')):
            self.assertEqual(atticus.meeting_gather({'prepared':'Read-only brief','event':{'id':'event'}}),{'brief':'Read-only brief'})

    def test_screen_wait_status_tracks_native_lease_marker(self):
        row=self.prepare();self.manager.start(row['id']);marker=Path(row['directory'])/'screen-wait';marker.touch()
        self.manager.tick();self.assertEqual(row['status'],'waiting-screen')
        marker.unlink();self.manager.tick();self.assertEqual(row['status'],'running')

    def test_allow_card_is_per_agent_and_answer_atomic(self):
        row=self.prepare();self.manager.start(row['id']);path=Path(row['directory'])
        (path/'q').write_text('{"q":"Allow?","cmd":"fixture","task":true}')
        self.manager.tick();self.assertEqual(row['status'],'needs-you');self.assertTrue(row['question']['task'])
        self.manager.command({'action':'answer','id':row['id'],'answer':'task'})
        self.assertEqual((path/'a').read_text(),'task');self.assertEqual((path/'a').stat().st_mode&0o777,0o600)
        self.assertEqual(row['status'],'running')

    def test_missing_setup_asks_once_then_waits_for_the_grant(self):
        with patch.dict(os.environ,{'ATTICUS_AGENT':'x','ATTICUS_ASK':self.root.name}),patch.object(atticus,'emit'):
            with patch.object(atticus,'ask_user',return_value='y') as ask:
                self.assertIn('on now',atticus.need('screen','To see the chart.'))
                self.assertEqual(ask.call_args[0][0]['need'],'screen');self.assertEqual(ask.call_args[0][0]['cmd'],'To see the chart.')
            with patch.object(atticus,'ask_user',return_value=None):
                self.assertIn('say in the result what you skipped',atticus.need('automation','To read Mail.','Mail'))
            # Atticus can turn page reading on itself: it asks "Do it?", or just does it when told to or when Settings says so
            with patch.object(atticus,'ask_user',return_value='n') as ask,patch.object(atticus,'allow_page_js',return_value=True) as js:
                self.assertIn('Not now',atticus.need('page-js','To read the page.','Google Chrome'));js.assert_not_called()
                self.assertIn('Turned on',atticus.need('page-js','To read the page.','Google Chrome',do=True));self.assertEqual(ask.call_count,1)
                self.prefs['fixSetup']=True;self.assertIn('Turned on',atticus.need('page-js'));self.assertEqual(ask.call_count,1)
        self.assertIn('Use one of',atticus.need('wifi'))
        row=self.prepare();self.manager.start(row['id']);path=Path(row['directory']);(path/'answers').mkdir()
        answer=str(path/'answers'/('c'*32));row.update(status='needs-you',question={'need':'screen','q':'Turn on Screen Recording','_a':answer})
        self.manager.command({'action':'answer','id':row['id'],'answer':'y','question':answer})   # Allow: Settings opens; the agent keeps waiting
        self.assertTrue(row['question']['waiting']);self.assertFalse(os.path.exists(answer));self.assertEqual(row['status'],'needs-you')
        self.manager.command({'action':'granted','id':row['id']})
        self.assertEqual(Path(answer).read_text(),'y');self.assertEqual(row['status'],'running')

    def test_missing_connector_asks_on_the_chip_and_resumes_once_connected(self):
        row=self.prepare('DONE fixture');self.manager.start(row['id']);self.until(lambda:row['status']=='done')
        result='No Gmail here. [agent-result {"summary":"Could not read Gmail.","missingConnector":"Gmail"}]'
        self.manager.finish(row,'done',result)
        self.assertEqual(row['status'],'needs-you');self.assertEqual(row['question']['need'],'connector');self.assertEqual(row['question']['app'],'Gmail')
        self.manager.command({'action':'answer','id':row['id'],'answer':'n','question':row['question']['_a']})
        self.assertEqual(row['status'],'done');self.assertIsNone(row['question'])   # Not now: the result already says what it skipped
        self.manager.finish(row,'done',result)
        states=iter([{'connected':['Drive'],'signin':['Gmail']},{'connected':['Drive','Gmail'],'signin':[]}])
        with patch.object(atticus,'connectors',side_effect=lambda:next(states)),patch.object(atticus.time,'sleep'):
            self.manager.command({'action':'answer','id':row['id'],'answer':'y','question':row['question']['_a']})
            self.until(lambda:any('connected Gmail' in e.get('text','') for e in atticus.read_rows(str(Path(row['directory'])/'log.jsonl')) if e.get('event')=='followup'))
        self.assertNotIn('connected Gmail',Path(row['directory'],'request').read_text())
        self.assertIsNone(row['question'])

    def test_connectors_reads_claude_mcp_list(self):
        out='Checking MCP server health…\n\nclaude.ai Gmail: https://g.example/mcp - ✔ Connected\nclaude.ai Canva: https://c.example/mcp - ! Needs authentication\nlocal: npx x - ✔ Connected\n'
        with patch.object(atticus.subprocess,'run',return_value=subprocess.CompletedProcess([],0,out,'')):
            self.assertEqual(atticus.connectors(),{'connected':['Gmail'],'signin':['Canva']})

    def test_stale_allow_click_cannot_answer_a_new_question(self):
        row=self.prepare();self.manager.start(row['id']);path=Path(row['directory']);(path/'answers').mkdir()
        row.update(status='needs-you',question={'q':'New permission','_a':str(path/'answers'/('b'*32))})
        self.manager.command({'action':'answer','id':row['id'],'answer':'y','question':str(path/'answers'/('a'*32))})
        self.assertEqual(row['status'],'needs-you');self.assertFalse((path/'answers'/('b'*32)).exists())
        self.manager.command({'action':'answer','id':row['id'],'answer':'n','question':str(path/'answers'/('b'*32))})
        self.assertEqual((path/'answers'/('b'*32)).read_text(),'n')

    def test_parallel_permission_hooks_do_not_share_answers(self):
        answers=[];root=Path(self.root.name)
        with patch.dict(os.environ,{'ATTICUS_ASK':self.root.name}):
            first=threading.Thread(target=lambda:answers.append(('first',atticus.ask_user({'q':'First'},3))))
            second=threading.Thread(target=lambda:answers.append(('second',atticus.ask_user({'q':'Second'},3))))
            first.start();self.until(lambda:(root/'q').exists());second.start();time.sleep(.1)
            self.assertEqual(json.loads((root/'q').read_text())['q'],'First')
            first_path=Path(json.loads((root/'q').read_text())['_a']);(root/'q').unlink();first_path.write_text('y');first.join(timeout=2)
            self.until(lambda:(root/'q').exists());self.assertEqual(json.loads((root/'q').read_text())['q'],'Second')
            second_path=Path(json.loads((root/'q').read_text())['_a']);self.assertNotEqual(first_path,second_path)
            (root/'q').unlink();second_path.write_text('n');second.join(timeout=2)
        self.assertEqual(answers,[('first','y'),('second','n')])
