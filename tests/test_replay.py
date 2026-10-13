"""Replay actions use scratch storage and fake app workers, never paid calls or personal apps."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import atticus


class ReplayTests(unittest.TestCase):
    def setUp(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup);self.root=Path(root.name)
        for p in [patch.object(atticus,'DIR',root.name),patch.object(atticus,'SKILLS',str(self.root/'skills.json')),
                  patch.object(atticus,'WORKED',str(self.root/'worked.json')),patch.object(atticus,'ME_FILE',str(self.root/'me.json')),patch.object(atticus,'pref',side_effect=lambda k,d:d),
                  patch.dict(os.environ,{'ATTICUS_ASK':root.name,'ATTICUS_FRONT':'Other Fixture','ATTICUS_FOCUS_GUARD':'1'},clear=True)]:
            p.start();self.addCleanup(p.stop)
        self.commands=['open -a Fixture','peekaboo menu click --app "com.atticus.fixture" --path "Fixture > Start meeting" --no-remote',
                       'osascript -e \'tell application "System Events" to get name of every window of process "Fixture"\'']

    def test_legacy_three_steps_upgrade_and_replay_native_actions_without_ai_or_reads(self):
        (self.root/'skills.json').write_text(json.dumps({'make a fixture meeting':{'cmds':self.commands,'reply':'Started.','mine':True}}))
        with patch.object(atticus,'show',return_value='opened Fixture in the background') as show,patch.object(atticus,'hands',return_value=('Clicked',True)) as hands,patch.object(atticus.subprocess,'run') as shell,patch.object(atticus,'start_claude') as ai:
            self.assertEqual(atticus.fast('Please start a new fixture meeting'),'Started.')
        show.assert_called_once_with('Fixture',True);hands.assert_called_once_with('menu','com.atticus.fixture','Fixture > Start meeting')
        shell.assert_not_called();ai.assert_not_called();self.assertEqual(len(atticus.skills()['make a fixture meeting']['cmds']),2)

    def test_new_shortcuts_drop_reads_and_keep_the_target_when_front_app_changes(self):
        with patch.object(atticus,'cached_hands',return_value=None): self.assertTrue(atticus.learn('make a fixture meeting',self.commands,'Started.'))
        commands=atticus.skills()['make a fixture meeting']['cmds']
        self.assertEqual(commands,[atticus.SHOW+' --bg Fixture',atticus.HANDS+" menu com.atticus.fixture 'Fixture > Start meeting'"])
        self.assertNotIn('Other Fixture',' '.join(commands))
        self.assertIsNone(atticus.shortcut_commands('export',['%s click "File > Export"'%atticus.HANDS]))

    def test_explicit_open_remains_foreground_and_mutating_scripts_are_kept(self):
        script='osascript -e \'tell application "Fixture" to set name of window 1 to "Done"\''
        self.assertEqual(atticus.shortcut_commands('open Fixture',['open -a Fixture',script]),['open -a Fixture',script])
        self.assertEqual(atticus.shortcut_commands('check',['osascript -e \'get "click"\'']),[])

    def test_unsupported_peekaboo_falls_back_without_running(self):
        atticus.save_skills({'do fixture':{'cmds':['peekaboo click --id 12'],'reply':'Done.','mine':True}})
        with patch.object(atticus.subprocess,'run') as run:self.assertIsNone(atticus.replay('do fixture'));run.assert_not_called()
        self.assertIsNone(atticus.shortcut_commands('do fixture',[self.commands[-1]+' && open -a Fixture']))
        self.assertIsNone(atticus.shortcut_commands('do fixture',[atticus.ME+' ask again']))
        reply,saved=atticus.remember_workflow('nested fixture',f'Done. [workflow nested fixture | Use the native app.] [replay python3 {atticus.ME} ask again]',True,['open Fixture'])
        self.assertTrue(saved);self.assertNotIn('nested fixture',atticus.skills())

    def test_native_menu_bypasses_map_capture_but_waits_for_confirmation(self):
        args=('menu','com.atticus.fixture','Fixture > Start meeting')
        worker=unittest.mock.Mock(returncode=0,stdout='Clicked Start meeting\n',stderr='')
        with patch.object(atticus,'wait_commit',return_value=True),patch.object(atticus,'hands_call',return_value=worker) as call,patch.object(atticus,'hands_map') as mapping:
            self.assertTrue(atticus.hands(*args)[1]);call.assert_called_once_with(list(args),timeout=2);mapping.assert_not_called()
        with patch.object(atticus,'wait_commit',return_value=False),patch.object(atticus,'hands_call') as call:
            self.assertFalse(atticus.hands(*args)[1]);call.assert_not_called()

    def test_guarded_background_open_does_not_poll_or_sleep(self):
        with patch.object(atticus,'osa') as osa,patch.object(atticus.time,'sleep') as sleep,patch.object(atticus,'preview_app'),patch.object(atticus.subprocess,'run',return_value=unittest.mock.Mock(returncode=0)):
            atticus.show('Fixture',bg=True);osa.assert_not_called();sleep.assert_not_called()

    def test_failed_direct_open_falls_back_instead_of_claiming_success(self):
        atticus.save_skills({'open fixture':{'cmds':[atticus.SHOW+' Fixture'],'reply':'Opened.','mine':True}})
        with patch.object(atticus,'show',return_value='no app or file called "Fixture"'):
            self.assertIsNone(atticus.replay('open fixture'))

    def test_recent_memory_creates_missing_scratch_directory(self):
        missing=self.root/'new';recent=missing/'recent.jsonl'
        with patch.object(atticus,'DIR',str(missing)),patch.object(atticus,'RECENT',str(recent)):
            atticus.remember_turn('Find fixture notes','Found them.');self.assertEqual(atticus.recent()[0]['q'],'Find fixture notes')
            self.assertEqual(recent.stat().st_mode&0o777,0o600)
            with patch.object(atticus.os,'replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError): atticus.remember_turn('Other ask','Other answer')
            self.assertEqual(atticus.recent()[0]['q'],'Find fixture notes')

    def test_first_ai_answer_saves_memory_and_log_in_a_fresh_directory(self):
        class NoThread:
            def __init__(self,*a,**kw): pass
            def start(self): pass
            def join(self,*a): pass
        missing=self.root/'fresh';brain=unittest.mock.Mock(return_value=('Found them.',0,[]));brain.ok=True;brain.cmds=[];brain.ran=[]
        with patch.object(atticus,'DIR',str(missing)),patch.object(atticus,'RECENT',str(missing/'recent.jsonl')),patch.object(atticus,'MEMORY',str(missing/'memory.md')),patch.object(atticus.threading,'Thread',NoThread),patch('sys.argv',['atticus.py','Find fixture notes']),patch.multiple(atticus,licensed=lambda:True,ping=lambda:None,fast=lambda _:None,front=lambda:('Fixture','Scratch'),osa=lambda *a:'',screens=lambda:[],describe=lambda *a:'Scratch display',selection_context=lambda:'',screen_for=lambda *a:None,run=brain,wait_commit=lambda:True,restore_focus=lambda *a:None,say=lambda *a:None,tip=lambda *a:None,look=lambda:[],hands_map=lambda *a:None):
            atticus.main();self.assertEqual(atticus.recent()[0]['a'],'Found them.');self.assertTrue((missing/'log.jsonl').exists())
        brain.assert_called_once()
