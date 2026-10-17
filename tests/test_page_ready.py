"""Page readiness uses fake browser calls and a controlled clock; no real apps or paid calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import atticus


class PageReadyTests(unittest.TestCase):
    def test_background_page_reuses_only_its_unchanged_tab_for_both_protocols(self):
        for adapter, target in [('chromium', 'first tab of window id 10 whose id is 2'), ('webkit', 'tab 2 of window id 10')]:
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as scratch:
                Path(scratch, 'background-page.json').write_text(json.dumps(
                    {'app': 'Browser Fixture', 'adapter': adapter, 'window': 10, 'tab': 2, 'url': 'https://old.invalid/'}))
                with patch.object(atticus, 'DIR', scratch), patch.dict(os.environ, {'ATTICUS_ASK': scratch}), \
                     patch.object(atticus, 'browser_adapter', return_value=adapter), \
                     patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                    self.assertTrue(atticus.background_tab('https://new.invalid/', 'Browser Fixture'))
                self.assertIn(target, run.call_args.args[0][2])
                self.assertIn('Atticus page changed', run.call_args.args[0][2])
                self.assertEqual(run.call_args.args[0][-2:], ['https://old.invalid/', 'https://new.invalid/'])
                page = json.loads(Path(scratch, 'web.json').read_text())
                self.assertEqual((page['window'], page['tab'], page['target']), (10, 2, None))
                self.assertEqual(Path(scratch, 'web.json').stat().st_mode & 0o777, 0o600)

    def test_background_page_changed_by_the_user_is_not_reused(self):
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch, 'background-page.json').write_text(json.dumps(
                {'app': 'Browser Fixture', 'adapter': 'chromium', 'window': 10, 'tab': 2, 'url': 'https://old.invalid/'}))
            refused = subprocess.CompletedProcess([], 1, '', 'Atticus page changed')
            with patch.object(atticus, 'DIR', scratch), patch.dict(os.environ, {'ATTICUS_ASK': scratch}), \
                 patch.object(atticus, 'browser_adapter', return_value='chromium'), patch.object(atticus.subprocess, 'run', return_value=refused):
                self.assertFalse(atticus.background_tab('https://new.invalid/', 'Browser Fixture'))
            self.assertFalse(Path(scratch, 'web.json').exists())

    def test_background_show_navigates_the_dedicated_tab_without_opening_another(self):
        handler = subprocess.CompletedProcess([], 0, 'Browser Fixture\n', '')
        with patch.dict(os.environ, {'ATTICUS_FOCUS_GUARD': '1'}), patch.object(atticus.subprocess, 'run', return_value=handler) as run, \
             patch.object(atticus, 'background_tab', return_value=True), patch.object(atticus, 'remember_background_tab') as remember, \
             patch.object(atticus, 'web', return_value='Page ready.') as web:
            self.assertEqual(atticus.show('https://fixture.invalid', bg=True), 'opened https://fixture.invalid in the background')
        self.assertEqual(run.call_args_list, [unittest.mock.call([atticus.APP, '--target-app', 'https://fixture.invalid'], capture_output=True, text=True, timeout=1)])
        web.assert_called_once_with('wait', text='https://fixture.invalid', app='Browser Fixture')
        remember.assert_called_once_with('Browser Fixture')

    def test_scriptable_page_adapter_is_probed_and_cached_without_an_app_list(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if args[:2] == ['pgrep', '-xq']: return subprocess.CompletedProcess(args, 0, '', '')
                script = args[2]
                if script.startswith('id of application'): return subprocess.CompletedProcess(args, 0, 'org.fixture.nebula\n', '')
                if 'return (id of window' in script: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                return subprocess.CompletedProcess(args, 0, '{"ready":true,"signature":"fixture"}', '')
            with patch.object(atticus, 'DIR', scratch), patch.object(atticus.subprocess, 'run', side_effect=worker) as run:
                self.assertEqual(atticus.browser_adapter('Nebula'), 'chromium')
                calls = run.call_count
                self.assertEqual(atticus.browser_adapter('Nebula'), 'chromium')
            self.assertEqual(run.call_count, calls + 2)   # running + bundle-id lookup only; no protocol probe
            cache = json.loads(Path(scratch, 'web-capabilities.json').read_text())
            self.assertEqual(cache['org.fixture.nebula'], 'chromium')

    def test_adapter_never_events_a_stopped_app_or_caches_permission_denials(self):
        with tempfile.TemporaryDirectory() as scratch:
            with patch.object(atticus, 'DIR', scratch), patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')) as run:
                self.assertEqual(atticus.browser_adapter('Stopped Fixture'), '')
            self.assertEqual(run.call_args.args[0], ['pgrep', '-xq', 'Stopped Fixture'])
            self.assertFalse(Path(scratch, 'web-capabilities.json').exists())

            probes = []
            def worker(args, **kwargs):
                if args[:2] == ['pgrep', '-xq']: return subprocess.CompletedProcess(args, 0, '', '')
                script = args[2]; probes.append(script)
                if script.startswith('id of application'): return subprocess.CompletedProcess(args, 0, 'org.fixture.native\n', '')
                if 'return (id of window' in script: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                return subprocess.CompletedProcess(args, 1, '', 'Not allowed to send Apple events')
            with patch.object(atticus, 'DIR', scratch), patch.object(atticus.subprocess, 'run', side_effect=worker):
                self.assertEqual(atticus.browser_adapter('Native Fixture'), '')
                count = len(probes)
                self.assertEqual(atticus.browser_adapter('Native Fixture'), '')
            self.assertGreater(len(probes), count + 1)
            self.assertFalse(Path(scratch, 'web-capabilities.json').exists())

    def test_any_javascript_permission_message_identifies_a_disabled_adapter(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if args[:2] == ['pgrep', '-xq']: return subprocess.CompletedProcess(args, 0, '', '')
                script = args[2]
                if script.startswith('id of application'): return subprocess.CompletedProcess(args, 0, 'org.fixture.browser\n', '')
                if 'return (id of window' in script: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                return subprocess.CompletedProcess(args, 1, '', 'You must enable Allow JavaScript from Apple Events')
            with patch.object(atticus, 'DIR', scratch), patch.object(atticus.subprocess, 'run', side_effect=worker):
                self.assertEqual(atticus.browser_adapter('Browser Fixture'), 'chromium')

    def test_window_query_permission_denial_is_retried_later(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if args[:2] == ['pgrep', '-xq']: return subprocess.CompletedProcess(args, 0, '', '')
                if args[2].startswith('id of application'): return subprocess.CompletedProcess(args, 0, 'org.fixture.browser\n', '')
                return subprocess.CompletedProcess(args, 1, '', 'Not authorized to send Apple events')
            with patch.object(atticus, 'DIR', scratch), patch.object(atticus.subprocess, 'run', side_effect=worker) as run:
                self.assertEqual(atticus.browser_adapter('Browser Fixture'), '')
                first = run.call_count
                self.assertEqual(atticus.browser_adapter('Browser Fixture'), '')
            self.assertGreater(run.call_count, first + 2)
            self.assertFalse(Path(scratch, 'web-capabilities.json').exists())

    def test_label_hides_flags_query_and_chained_commands(self):
        for tail in ('', ' && sleep 3 && '+atticus.WEB+' read', '; '+atticus.WEB+' read'):
            self.assertEqual(atticus.label('Bash',{'command':atticus.SHOW+' --bg "https://www.amazon.com/s?k=epsom+salt"'+tail}),'Opening amazon.com')
        self.assertEqual(atticus.label('Bash',{'command':atticus.SHOW+' --bg "/tmp/Fixture Notes.md" && echo ok'}),'Opening Fixture Notes.md')

    def test_poll_waits_for_ready_and_stable_dom_with_a_five_second_cap(self):
        now=[0];seen=[]
        def sleep(seconds): now[0]+=seconds
        def read(timeout):
            seen.append(timeout);return json.dumps({'ready':now[0]>=.2,'signature':'loaded' if now[0]>=.3 else 'old'})
        with patch.object(atticus.time,'monotonic',side_effect=lambda:now[0]),patch.object(atticus.time,'sleep',side_effect=sleep):
            self.assertTrue(atticus.page_ready(read));self.assertLess(now[0],.7);self.assertGreaterEqual(now[0],.5)
            now[0]=0;self.assertFalse(atticus.page_ready(lambda timeout:json.dumps({'ready':False})));self.assertLessEqual(now[0],5.01)
        self.assertTrue(all(0<timeout<=.5 for timeout in seen))

    def test_an_already_loaded_other_tab_is_not_ready(self):
        now=[0]
        def sleep(seconds): now[0]+=seconds
        def read(timeout): return json.dumps({'ready':True,'signature':('https://mail.google.com/x|1' if now[0]<1 else 'https://www.amazon.com/s?k=x|2')})
        with patch.object(atticus.time,'monotonic',side_effect=lambda:now[0]),patch.object(atticus.time,'sleep',side_effect=sleep):
            self.assertTrue(atticus.page_ready(read,'https://amazon.com/s?k=x'));self.assertGreaterEqual(now[0],1)   # Gmail was loaded, but Amazon is what we opened

    def test_permission_error_is_not_hidden_by_polling(self):
        with patch.object(atticus.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'blocks this'):atticus.page_ready(Mock(side_effect=RuntimeError('Browser blocks this. Enable JavaScript.')))
            sleep.assert_not_called()

    def test_background_url_waits_in_its_handler_browser(self):
        with patch.dict(os.environ,{'ATTICUS_FOCUS_GUARD':'1'}),patch.object(atticus.subprocess,'run',return_value=Mock(returncode=0)),patch.object(atticus,'preview_app',return_value={'app':'Safari'}),patch.object(atticus,'web',return_value='Page ready.') as web:
            self.assertEqual(atticus.show('https://fixture.invalid',bg=True),'opened https://fixture.invalid in the background')
        web.assert_called_once_with('wait',text='https://fixture.invalid',app='Safari',fresh=True)

    def test_read_after_open_keeps_the_handler_browser_instead_of_another_running_browser(self):
        with tempfile.TemporaryDirectory() as scratch,patch.dict(os.environ,{'ATTICUS_ASK':scratch,'ATTICUS_FRONT':'Google Chrome'}),patch.object(atticus,'browser_adapter',return_value='webkit'),patch.object(atticus,'page_ready',return_value=True),patch.object(atticus,'preview_app',return_value=None),patch.object(atticus,'hands_map',return_value=None),patch.object(atticus.subprocess,'run',side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,'10\t2' if 'return (id of window' in args[2] else 'Fixture, 0, 0, 300, 200' if 'get {name, bounds}' in args[2] else 'Fixture page','')) as run:
            self.assertEqual(atticus.web('wait',app='Safari',fresh=True),'Page ready.')
            self.assertEqual(atticus.web('read'),'Fixture page')
            self.assertIn('in tab 2 of window id 10',run.call_args.args[0][2])
            self.assertNotIn('front document',run.call_args.args[0][2])
            self.assertEqual(sum('return (id of window' in c.args[0][2] for c in run.call_args_list),1)

    def test_agent_hands_and_later_previews_stay_on_the_pinned_page_window(self):
        target={'pid':123,'windowID':'77','windowFrame':[10,20,600,400],'window':'Work','app':'Browser Fixture'}
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch,'web.json').write_text(json.dumps({'app':'Browser Fixture','adapter':'webkit','window':10,'tab':2,'target':None}))
            process=Mock(returncode=0,args=[]);process.communicate.return_value=('mapped','');process.poll.return_value=0
            def browser(args,**kwargs):
                return subprocess.CompletedProcess(args,0,'Work, 10, 20, 610, 420' if 'get {name, bounds}' in args[2] else 'Fixture page','')
            with patch.dict(os.environ,{'ATTICUS_ASK':scratch,'ATTICUS_AGENT':'agent','ATTICUS_HANDS_STATE':str(Path(scratch,'hands.json'))}), \
                 patch.object(atticus,'browser_adapter',return_value='webkit'),patch.object(atticus,'preview_app',return_value=target), \
                 patch.object(atticus.subprocess,'run',side_effect=browser),patch.object(atticus.subprocess,'Popen',return_value=process) as popen:
                self.assertEqual(atticus.web('read'),'Fixture page')
            self.assertEqual(popen.call_args.kwargs['env']['ATTICUS_HANDS_WINDOW'],'77')
            Path(scratch,'hands.json').write_text(json.dumps(target))
            with patch.dict(os.environ,{'ATTICUS_AGENT':'agent','ATTICUS_HANDS_STATE':str(Path(scratch,'hands.json'))}), \
                 patch.object(atticus.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(target),'')) as run:
                atticus.preview_app('Browser Fixture')
            self.assertEqual(json.loads(run.call_args.args[0][-1]),target['windowFrame'])

    def test_readiness_failure_after_a_click_keeps_success_without_repeating(self):
        with tempfile.TemporaryDirectory() as scratch:
            def run(args,**kwargs):
                if 'return (id of window' in args[2]: return subprocess.CompletedProcess(args,0,'10\t2','')
                if 'get {name, bounds}' in args[2]: return subprocess.CompletedProcess(args,0,'Fixture, 0, 0, 300, 200','')
                return subprocess.CompletedProcess(args,0,json.dumps({'what':'Go','short':'Go','url':'https://fixture.invalid'}) if '"name"' in args[-1] else 'Clicked 1: Go','')
            with patch.dict(os.environ,{'ATTICUS_FRONT':'Safari','ATTICUS_ASK':scratch}),patch.object(atticus,'browser_adapter',return_value='webkit'),patch.object(atticus,'preview_app',return_value=None),patch.object(atticus,'hands_map',return_value=None),patch.object(atticus.subprocess,'run',side_effect=run) as worker,patch.object(atticus,'page_ready',side_effect=subprocess.TimeoutExpired('fixture',.5)):
                result=atticus.web('click','1')
            self.assertTrue(result.startswith('Clicked 1'));self.assertIn('Read again',result)
            self.assertEqual(sum('"click"' in call.args[0][-1] for call in worker.call_args_list),1)


    def test_form_submission_always_requires_confirmation_even_with_full_access(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if 'return (id of window' in args[2]: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                if 'get {name, bounds}' in args[2]: return subprocess.CompletedProcess(args, 0, 'Fixture, 0, 0, 300, 200', '')
                return subprocess.CompletedProcess(args, 0, json.dumps({'what': 'Go', 'short': 'Go', 'url': 'https://fixture.invalid', 'submit': True}), '')
            with patch.dict(os.environ, {'ATTICUS_FRONT': 'Safari', 'ATTICUS_ASK': scratch}), patch.object(atticus, 'browser_adapter', return_value='webkit'), patch.object(atticus, 'pref', return_value=False), patch.object(atticus, 'preview_app', return_value=None), patch.object(atticus.subprocess, 'run', side_effect=worker) as run, patch.object(atticus, 'ask_user', return_value='n') as ask:
                self.assertIn('user said no', atticus.web('click', '1'))
            ask.assert_called_once()
            self.assertFalse(any(')("click"' in call.args[0][-1] for call in run.call_args_list))

    def test_shortcut_submit_refusal_does_not_claim_safe_mode_is_on(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if 'return (id of window' in args[2]: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                if 'get {name, bounds}' in args[2]: return subprocess.CompletedProcess(args, 0, 'Fixture, 0, 0, 300, 200', '')
                return subprocess.CompletedProcess(args, 0, json.dumps({'what': 'Submit', 'short': 'Submit', 'url': 'https://fixture.invalid', 'submit': True}), '')
            with patch.dict(os.environ, {'ATTICUS_FRONT': 'Safari'}, clear=True), patch.object(atticus, 'DIR', scratch), \
                 patch.object(atticus, 'browser_adapter', return_value='webkit'), patch.object(atticus, 'pref', return_value=False), \
                 patch.object(atticus, 'preview_app', return_value=None), patch.object(atticus.subprocess, 'run', side_effect=worker), \
                 patch.object(atticus, 'ask_user') as ask:
                result = atticus.web('click', '1')
            self.assertIn('needs the user', result)
            self.assertNotIn('Safe mode', result)
            ask.assert_not_called()

    def test_fill_returns_current_field_and_page_state_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as scratch:
            def worker(args, **kwargs):
                if 'return (id of window' in args[2]: return subprocess.CompletedProcess(args, 0, '10\t2', '')
                if 'get {name, bounds}' in args[2]: return subprocess.CompletedProcess(args, 0, 'Fixture, 0, 0, 300, 200', '')
                expression = args[-1]
                output = '{"value":"Current answer"}' if ')("state"' in expression else 'Filled 1' if ')("fill"' in expression else 'Changed page'
                return subprocess.CompletedProcess(args, 0, output, '')
            with patch.dict(os.environ, {'ATTICUS_FRONT': 'Safari', 'ATTICUS_ASK': scratch}), patch.object(atticus, 'browser_adapter', return_value='webkit'), patch.object(atticus, 'preview_app', return_value=None), patch.object(atticus.subprocess, 'run', side_effect=worker), patch.object(atticus, 'page_ready', return_value=True):
                result = atticus.web('fill', '1', 'Answer')
        self.assertIn('Current answer', result)
        self.assertIn('Changed state:', result)
