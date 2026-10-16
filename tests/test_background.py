"""Background tasks use fake workers; no user apps, windows, or configuration."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        for p in [patch.dict(os.environ, {'ATTICUS_ASK': root.name}, clear=True),
                  patch.object(atticus, 'WORKED', str(Path(root.name, 'worked.json'))),
                  patch.object(atticus, 'wait_commit', return_value=True),
                  patch.object(atticus, 'pref', side_effect=lambda k, d: d)]:
            p.start(); self.addCleanup(p.stop)
        self.target = {'pid': 123, 'windowID': '42', 'windowFrame': [0, 0, 300, 200], 'app': 'Notes', 'window': 'Fixture'}

    def gate(self, cmd):
        with patch('sys.stdin', io.StringIO(json.dumps({'tool_input': {'command': cmd}}))), patch('sys.stdout', new_callable=io.StringIO) as out, patch.object(atticus, 'preview_app'):
            atticus.gate()
        return json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'] if out.getvalue() else 'allow'

    def test_recursive_ask_and_unrequested_activation_are_denied(self):
        self.assertEqual(self.gate(f'env -u ATTICUS_ASK -u CLAUDECODE python3 {atticus.ME} ask again'), 'deny')
        self.assertEqual(self.gate(f'python3 {atticus.ME} --wait'), 'deny')
        self.assertEqual(self.gate(f'env -u ATTICUS_ASK {atticus.ME} ask again'), 'deny')
        self.assertEqual(self.gate(f'xargs {atticus.ME} ask again'), 'deny')
        self.assertEqual(self.gate(f'printf again | xargs {atticus.ME} ask'), 'deny')
        self.assertEqual(self.gate(f'echo ready && env {atticus.ME} ask again'), 'deny')
        self.assertEqual(self.gate(f'python3 {atticus.ME} --hands see Notes'), 'allow')
        self.assertEqual(self.gate('osascript -e \'tell application "Notes" to activate\''), 'deny')
        self.assertEqual(self.gate('open -a Notes'), 'deny')
        self.assertEqual(self.gate('env open -a Notes'), 'deny')
        self.assertEqual(self.gate('xargs open -a Notes'), 'deny')
        self.assertEqual(self.gate('printf Notes | xargs open -a'), 'deny')
        self.assertEqual(self.gate('xargs open -g -a Notes'), 'allow')
        self.assertEqual(self.gate('open -g -a Notes'), 'allow')
        self.assertEqual(self.gate('echo open'), 'allow')
        self.assertEqual(self.gate('printf open'), 'allow')
        self.assertEqual(self.gate(f'{atticus.HANDS} type Field activate'), 'allow')
        self.assertEqual(self.gate(f'{atticus.HANDS} type Field atticus.py'), 'allow')
        self.assertFalse(atticus.wants_focus('Do not show the window, just tell me whether it is open'))
        self.assertFalse(atticus.wants_focus('Do not open Notes; add a note'))
        self.assertFalse(atticus.wants_focus("Don't focus Notes"))
        self.assertFalse(atticus.wants_focus('Open Notes in the background'))
        Path(os.environ['ATTICUS_ASK'], 'request').write_text('Show Notes')
        self.assertEqual(self.gate('osascript -e \'tell application "Notes" to activate\''), 'allow')
        self.assertEqual(self.gate('printf Notes | xargs open -a'), 'allow')

    def test_one_helper_targets_every_app_and_records_last_window(self):
        with patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.target))) as run:
            for app in ['Spotify', 'Notes', 'Reminders', 'Finder']:
                atticus.preview_commands(f'osascript -e \'tell application "{app}" to make new item\'')
                self.assertEqual(run.call_args.args[0], [atticus.APP, '--window-target', app, ''])
        self.assertEqual(json.loads(Path(atticus.WORKED).read_text())['window'], 'Fixture')
        self.assertEqual(len(list(Path(os.environ['ATTICUS_ASK']).glob('hands-*'))), 4)

    def test_background_open_handles_apps_files_and_schemes(self):
        with patch.object(atticus, 'front', return_value=('Editor', 'Work')), patch.object(atticus, 'osa', return_value='Editor'), patch.object(atticus, 'preview_app',return_value=None) as preview, patch.object(atticus.time, 'sleep'), patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Notes.app')) as run:
            atticus.show('Notes', bg=True)
            self.assertEqual(run.call_args.args[0], ['open', '-g', '-a', 'Notes'])
            preview.assert_called_with('Notes', 'open')
            self.assertFalse(any('set frontmost' in str(c) for c in atticus.osa.call_args_list))   # Notes stayed behind: nothing to give back
            atticus.show('spotify:search:jazz', bg=True)
            self.assertEqual(run.call_args.args[0], ['open', '-g', 'spotify:search:jazz'])
            atticus.show(os.environ['ATTICUS_ASK'], bg=True)
            self.assertEqual(run.call_args.args[0], ['open', '-g', os.environ['ATTICUS_ASK']])

    def test_safari_web_emits_preview_without_raising(self):
        with patch.dict(os.environ, {'ATTICUS_FRONT': 'Safari'}), patch.object(atticus, 'browser_adapter',return_value='webkit'), patch.object(atticus, 'preview_app',return_value=None) as preview, patch.object(atticus,'hands_map',return_value=None), patch.object(atticus.subprocess, 'run', side_effect=lambda args,**kw:subprocess.CompletedProcess([], 0, '10\t2' if 'return (id of window' in args[2] else 'Fixture, 0, 0, 300, 200' if 'get {name, bounds}' in args[2] else 'page', '')) as run:
            self.assertEqual(atticus.web('read'), 'page')
            preview.assert_called_once_with('Safari', 'read', title='Fixture', frame=[0,0,300,200])
            self.assertNotIn('activate', run.call_args.args[0][2])

    def test_blocked_fetch_can_read_the_named_url_in_a_background_browser_with_preview(self):
        class Blocked(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503);self.end_headers();self.wfile.write(b'Fetch blocked')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Blocked);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}/requested-page'
        with self.assertRaises(urllib.error.HTTPError) as failure:atticus.get(url)
        self.assertEqual(failure.exception.code,503)
        def result(args,**kw):
            if 'make new tab' in args[2]:
                self.assertIn('set priorSelection to current tab of w',args[2])
                self.assertIn('set current tab of w to priorSelection',args[2])
            return subprocess.CompletedProcess([],0,'10\t2' if 'make new tab' in args[2] else 'Requested page, 0, 0, 300, 200' if 'get {name, bounds}' in args[2] else
                                               json.dumps({'signature':url+'|ready'}) if '"ready", ' in args[-1][-35:] else 'Actual requested page content', '')
        with patch.object(atticus,'browser_adapter',return_value='webkit'), patch.object(atticus,'page_ready',return_value=True), \
             patch.object(atticus,'preview_app',return_value=self.target),patch.object(atticus,'hands_map'),patch.object(atticus,'emit') as emit,patch.object(atticus.subprocess,'run',side_effect=result):
            self.assertIn('Actual requested page content',atticus.web('read',url,app='Fixture browser'))
            self.assertTrue(any(line.startswith('▣ ') for call in emit.call_args_list for line in call.args[0]))
        with patch.object(atticus,'show') as show:
            self.assertIn('without credentials',atticus.web('read','https://user:secret@fixture.example'))
            show.assert_not_called()
        with patch.object(atticus,'browser_adapter',return_value='webkit'),patch.object(atticus,'page_ready',return_value=False),patch.object(atticus.subprocess,'run',side_effect=result),patch.object(atticus,'preview_app'):
            self.assertIn('Requested page not ready',atticus.web('read',url,app='Fixture browser'))

    def test_restore_does_not_interrupt_a_new_unrelated_app(self):
        with patch.object(atticus, 'osa', return_value='Editor') as osa:
            atticus.restore_focus('Finder', 'make a note', ['tell application "Notes" to make new note'])
            self.assertEqual(osa.call_count, 1)

    def test_work_mode_covers_non_browser_apps(self):
        for mode in ('back', 'front'):
            with self.subTest(mode=mode), patch.object(atticus, 'pref', side_effect=lambda k, d: mode if k == 'workIn' else d), \
                 patch.object(atticus, 'osa', return_value='Editor') as osa, patch.object(atticus, 'preview_app', return_value={'app': 'Fixture'}), \
                 patch.object(atticus, 'keep_behind') as behind, patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(atticus.show('Fixture', bg=True), 'opened Fixture ' + ('in front' if mode == 'front' else 'in the background'))
                self.assertEqual(run.call_args.args[0], ['open'] + ([] if mode == 'front' else ['-g']) + ['-a', 'Fixture'])
                self.assertEqual(behind.call_count, int(mode == 'back'))
                osa.reset_mock()
                osa.return_value = 'Fixture'
                atticus.restore_focus('Editor', 'work in Fixture', ['tell application "Fixture" to work'])
                self.assertEqual(osa.call_count, 0 if mode == 'front' else 2)

    def test_preview_passes_explicit_focus_intent_to_native_apps(self):
        for requested in (False, True):
            with self.subTest(requested=requested), patch.object(atticus, 'wants_focus', return_value=requested), \
                 patch.object(atticus, 'emit'), patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.target))) as run:
                atticus.preview_app('Fixture')
                self.assertEqual(run.call_args.kwargs['env']['ATTICUS_FOCUS_REQUESTED'], '1' if requested else '0')

    def test_explicit_focus_is_not_undone_by_the_shared_fallback(self):
        with patch.dict(os.environ, {'ATTICUS_REQUEST': 'focus Fixture'}), patch.object(atticus, 'osa') as osa:
            atticus.keep_behind('Editor', 'Fixture')
            osa.assert_not_called()

    def test_parallel_agent_reads_own_tabs_and_never_launch_select_or_raise(self):
        barrier=threading.Barrier(3); pins={}; results={}; calls=[]
        def result(args,**kw):
            calls.append(args)
            self.assertEqual(args[0],'osascript')
            script=args[2]
            self.assertNotRegex(script,r'(?i)activate|AXRaise|frontmost|make new window|set (?:index|bounds|miniaturized)')
            url='https://fixture.example/'+threading.current_thread().name
            if 'make new tab' in script:
                self.assertIn('set priorSelection to active tab index of w',script)
                self.assertIn('set active tab index of w to priorSelection',script)
                pins[threading.current_thread().name]=len(pins)+1
                barrier.wait(timeout=3)
                return subprocess.CompletedProcess(args,0,'10\t'+str(pins[threading.current_thread().name]),'')
            if 'get {name, bounds}' in script: out='Fixture, 0, 0, 300, 200'
            else:
                self.assertIn('whose id is '+str(pins[threading.current_thread().name]),script)
                out=json.dumps({'signature':url+'|ready'}) if '"ready", ' in args[-1][-35:] else url
            return subprocess.CompletedProcess(args,0,out,'')
        with patch.dict(os.environ,{'ATTICUS_AGENT':'fixture'}),patch.object(atticus,'browser_adapter',return_value='chromium'),patch.object(atticus,'page_ready',return_value=True),patch.object(atticus,'preview_app',return_value=None),patch.object(atticus,'hands_map'),patch.object(atticus.subprocess,'run',side_effect=result):
            def read():
                name=threading.current_thread().name
                results[name]=atticus.web('read','https://fixture.example/'+name,app='Fixture Browser')
            threads=[threading.Thread(target=read,name=str(i)) for i in range(3)]
            for t in threads:t.start()
            for t in threads:t.join(5)
            self.assertEqual(results,{str(i):'https://fixture.example/'+str(i) for i in range(3)})
            self.assertFalse(atticus.wants_focus('Show this browser'))
            self.assertEqual(len(set(pins.values())),3)
            path=Path(os.environ['ATTICUS_ASK'],'web.json');saved=json.loads(path.read_text())
            self.assertIn(saved['tab'],pins.values())
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_agent_site_placement_uses_main_open_path_only_when_requested(self):
        url = 'https://unseen.example/catalog'
        with patch.dict(os.environ, {'ATTICUS_AGENT': 'fixture'}), \
             patch.object(atticus, 'pref', side_effect=lambda k, d: 'front' if k == 'workIn' else d), \
             patch.object(atticus, 'show', return_value='opened ' + url + ' in front') as show, \
             patch.object(atticus, 'browser_adapter', return_value=''):
            atticus.web('read', url, app='Unseen Browser')
            show.assert_called_once_with(url, bg=True)
            self.assertTrue(atticus.wants_focus(sites=True))
            self.assertFalse(atticus.wants_focus('Show this browser'))
            Path(os.environ['ATTICUS_ASK'], 'request').write_text('Research in the background')
            show.reset_mock()
            atticus.web('read', url, app='Unseen Browser')
            show.assert_not_called()

    def test_agent_show_in_front_does_not_reenter_url_reader(self):
        with patch.dict(os.environ, {'ATTICUS_AGENT': 'fixture'}), \
             patch.object(atticus, 'DIR', os.environ['ATTICUS_ASK']), \
             patch.object(atticus, 'pref', side_effect=lambda k, d: 'front' if k == 'workIn' else d), \
             patch.object(atticus, 'osa', return_value='Fixture Editor'), \
             patch.object(atticus, 'preview_app', return_value={'app': 'Unseen Browser'}), \
             patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
             patch.object(atticus, 'web', return_value='Page ready.') as web:
            self.assertIn('in front', atticus.show('https://unseen.example', bg=True))
            self.assertIn(unittest.mock.call(['open', 'https://unseen.example']), run.call_args_list)
            web.assert_called_once_with('wait', text='https://unseen.example', app='Unseen Browser', fresh=True)

    def test_agent_background_show_and_window_reads_preserve_their_pin(self):
        Path(os.environ['ATTICUS_ASK'],'window-summary.json').write_text(json.dumps({'app':'Fixture Browser','web':True}))
        with patch.dict(os.environ,{'ATTICUS_AGENT':'fixture'}),patch.object(atticus,'web',return_value='Page\nControls:') as web:
            atticus.show('https://fixture.example')
            web.assert_called_with('read','https://fixture.example')
            atticus.window_text('Fixture Browser')
            web.assert_called_with('read',app='Fixture Browser',fresh=False)

    def test_front_defaults_to_background_and_show_respects_user_intent(self):
        with patch.object(atticus, 'preview_app',return_value=None) as preview, patch.object(atticus, 'hands_map', return_value={'description': 'Notes map'}), patch.object(atticus, 'osa') as osa, patch('sys.argv', ['atticus.py', '--front', 'Notes']), patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.main()
            osa.assert_not_called(); self.assertEqual(out.getvalue().strip(), 'Notes map')
            preview.assert_called_once()
        with patch.object(atticus, 'show') as show, patch('sys.stdout', new_callable=io.StringIO), patch('sys.argv', ['atticus.py', '--show', 'Notes']):
            atticus.main(); show.assert_called_with('Notes', True)
            Path(os.environ['ATTICUS_ASK'], 'request').write_text('Open Notes')
            atticus.main(); show.assert_called_with('Notes', False)

    def test_old_foreground_shortcuts_upgrade_to_background_open(self):
        saved={'cmds':['open -a Notes'],'reply':'Done.'}
        with patch.object(atticus,'skills',return_value={'study':saved}),patch.object(atticus,'show',return_value='opened Notes in the background') as show:
            self.assertEqual(atticus.replay('study'),'Done.');show.assert_called_once_with('Notes',True)

    def test_open_preview_expands_shell_home_and_restore_uses_current_app(self):
        with patch.object(atticus,'preview_app') as preview:
            atticus.preview_commands('open -g ~/Documents/fixture.pdf')
            preview.assert_called_with(os.path.expanduser('~/Documents/fixture.pdf'),'open',directory=None)
        with patch.dict(os.environ,{'ATTICUS_FRONT':'Old Mail'}),patch.object(atticus,'osa',side_effect=['Editor','com.fixture.browser','Browser','Browser','']) as osa,patch.object(atticus,'preview_app',return_value={'app':'Browser'}),patch.object(atticus.time,'sleep'),patch.object(atticus.subprocess,'run',return_value=subprocess.CompletedProcess([],0)):
            atticus.show('https://fixture.invalid',bg=True)
            self.assertEqual(osa.call_args.args[1],'Editor')

    def test_browser_preview_stays_with_the_pinned_window_after_focus_changes(self):
        with patch.dict(os.environ,{'ATTICUS_FRONT':'Google Chrome'}),patch.object(atticus,'browser_adapter',return_value='chromium'),patch.object(atticus,'preview_app',return_value=self.target) as preview,patch.object(atticus,'page_ready',return_value=True),patch.object(atticus,'hands_map',return_value=None),patch.object(atticus,'emit') as emit,patch.object(atticus.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0,'41\t73',''),subprocess.CompletedProcess([],0,'Fixture, 0, 0, 300, 200',''),subprocess.CompletedProcess([],0,'Page',''),subprocess.CompletedProcess([],0,'Filled',''),subprocess.CompletedProcess([],0,'Current field value',''),subprocess.CompletedProcess([],0,'Fixture, 10, 20, 410, 320',''),subprocess.CompletedProcess([],0,'Reread','')]) as run:
            atticus.web('read');atticus.web('fill','2','Text');atticus.web('read')
        self.assertIn('window id 41 whose id is 73',run.call_args.args[0][2])
        self.assertEqual(preview.call_args_list, [unittest.mock.call('Google Chrome','read',title='Fixture',frame=[0,0,300,200]),unittest.mock.call('Google Chrome','read',title='Fixture',frame=[10,20,400,300])])
        target=json.loads(emit.call_args.args[0][0][2:]);self.assertEqual(target['windowID'],'42');self.assertEqual(target['action'],'read')

    def test_applescript_ids_and_named_windows_use_the_same_target_helper(self):
        with patch.object(atticus,'preview_app') as preview:
            atticus.preview_commands('osascript -e \'tell application id "com.apple.Notes" to set name of window "Pilot" to "Pilot"\'')
            preview.assert_called_once_with('com.apple.Notes',title='Pilot',directory=None)
