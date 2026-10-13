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
        with tempfile.TemporaryDirectory() as scratch,patch.dict(os.environ,{'ATTICUS_ASK':scratch,'ATTICUS_FRONT':'Google Chrome'}),patch.object(atticus,'page_ready',return_value=True),patch.object(atticus,'preview_app',return_value=None),patch.object(atticus,'hands_map',return_value=None),patch.object(atticus.subprocess,'run',side_effect=lambda args,**kw:subprocess.CompletedProcess([],0,'10\t2' if 'return (id of window' in args[2] else 'Fixture page','')) as run:
            self.assertEqual(atticus.web('wait',app='Safari',fresh=True),'Page ready.')
            self.assertEqual(atticus.web('read'),'Fixture page')
            self.assertIn('in tab 2 of window id 10',run.call_args.args[0][2])
            self.assertNotIn('front document',run.call_args.args[0][2])
            self.assertEqual(sum('return (id of window' in c.args[0][2] for c in run.call_args_list),1)

    def test_readiness_failure_after_a_click_keeps_success_without_repeating(self):
        with tempfile.TemporaryDirectory() as scratch:
            def run(args,**kwargs):
                if 'return (id of window' in args[2]: return subprocess.CompletedProcess(args,0,'10\t2','')
                return subprocess.CompletedProcess(args,0,json.dumps({'what':'Go','short':'Go','url':'https://fixture.invalid'}) if '"name"' in args[-1] else 'Clicked 1: Go','')
            with patch.dict(os.environ,{'ATTICUS_FRONT':'Safari','ATTICUS_ASK':scratch}),patch.object(atticus,'preview_app',return_value=None),patch.object(atticus,'hands_map',return_value=None),patch.object(atticus.subprocess,'run',side_effect=run) as worker,patch.object(atticus,'page_ready',side_effect=subprocess.TimeoutExpired('fixture',.5)):
                result=atticus.web('click','1')
            self.assertTrue(result.startswith('Clicked 1'));self.assertIn('Read again',result)
            self.assertEqual(sum('"click"' in call.args[0][-1] for call in worker.call_args_list),1)
