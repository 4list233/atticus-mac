"""Background tasks use fake workers; no user apps, windows, or configuration."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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

    def test_restore_does_not_interrupt_a_new_unrelated_app(self):
        with patch.object(atticus, 'osa', return_value='Editor') as osa:
            atticus.restore_focus('Finder', 'make a note', ['tell application "Notes" to make new note'])
            self.assertEqual(osa.call_count, 1)

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
