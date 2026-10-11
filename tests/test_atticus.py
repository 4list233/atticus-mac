"""Offline regressions: no Claude calls, Mac actions, or user settings."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class BrainTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()   # never the user's real log, chat or rules (and CI has no ~/.config/atticus)
        self.addCleanup(root.cleanup)
        p = patch.object(atticus, 'DIR', root.name)
        p.start()
        self.addCleanup(p.stop)
        self.prefs = patch.object(atticus, 'pref', side_effect=lambda k, d: d)
        self.prefs.start()
        self.addCleanup(self.prefs.stop)

    def test_domain_dots_and_url_case_survive(self):
        with patch.object(atticus, 'skills', return_value={}), patch.object(atticus.subprocess, 'run') as run:
            for request, url in [('Open github.com.', 'https://github.com'),
                                 ('Open github dot com', 'https://github.com'),
                                 ('Open https://github.com/SaarthurR/atticus?q=UI', 'https://github.com/SaarthurR/atticus?q=UI')]:
                self.assertTrue(atticus.fast(request))
                self.assertEqual(run.call_args.args[0], ['open', url])

    def test_close_and_quit_do_not_restore_focus(self):
        with patch.object(atticus, 'osa') as osa:
            for task in ['Close Spotify', 'quit Spotify', 'exit Spotify', 'close this window']:
                atticus.restore_focus('Spotify', task, [])
            osa.assert_not_called()

    def test_focus_restore_only_addresses_existing_process(self):
        with patch.object(atticus, 'osa') as osa:
            atticus.restore_focus('Spotify', 'what time is it', [])
            script, app = osa.call_args.args
            self.assertEqual(app, 'Spotify')
            self.assertIn('if exists process (item 1 of argv) then', script)
            self.assertIn('set frontmost of process', script)
            self.assertNotIn('activate', script)
            self.assertNotIn('tell application "Spotify"', script)

    def test_explicit_focus_and_front_commands_keep_their_destination(self):
        with patch.object(atticus, 'osa') as osa:
            atticus.restore_focus('Notes', 'switch to Spotify', [])
            atticus.restore_focus('Notes', 'show me Spotify', [atticus.FRONT + ' Spotify'])
            atticus.restore_focus('Notes', 'open Spotify', [atticus.SHOW + ' Spotify'])
            osa.assert_not_called()

    def test_missing_display_does_not_send_wrong_screen(self):
        snap = unittest.mock.Mock()
        snap.shots = [('monitor.jpg', 'Screen 2: external monitor')]
        displays = [('Laptop', 0, 0, 1400, 900, True), ('Monitor', 1400, 0, 1900, 1000, False)]
        with patch.object(atticus, 'pref', side_effect=lambda k, d: d), patch.object(atticus, 'screens', return_value=displays):
            self.assertEqual(atticus.screen_for('read this screen', snap, 50), [])
            self.assertEqual(atticus.screen_for('read this screen', snap, 1500), [snap.shots[0]])
            self.assertIsNone(atticus.screen_for('open notes', snap, 50))

    def test_commit_waits_for_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'commit')
            timer = threading.Timer(0.15, lambda: Path(path).write_text('go'))
            with patch.dict(os.environ, {'ATTICUS_COMMIT': path}):
                timer.start()
                try:
                    started = time.monotonic()
                    self.assertTrue(atticus.wait_commit())
                    self.assertGreaterEqual(time.monotonic() - started, 0.15)
                finally:
                    timer.join()

    def test_gate_denies_unconfirmed_request(self):
        with patch.object(atticus, 'wait_commit', return_value=False), \
             patch('sys.stdin', io.StringIO(json.dumps({'tool_input': {'command': 'open -a Notes'}}))), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.gate()
            self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cancelled_parent_cannot_use_leftover_commit(self):
        with patch.dict(os.environ, {'ATTICUS_COMMIT': '/fake/commit'}), \
             patch.object(atticus.os, 'getppid', side_effect=[100, 101]), \
             patch.object(atticus.os.path, 'exists', return_value=True):
            self.assertFalse(atticus.wait_commit())

    def test_live_does_not_replay_learned_shell(self):
        with patch.dict(os.environ, {'ATTICUS_LIVE': '1'}), \
             patch.object(atticus, 'skills', return_value={k: {'cmds': ['echo private'], 'reply': 'Done'} for k in ['daily mode', 'open spotify']}), \
             patch.object(atticus.subprocess, 'run') as run:
            self.assertIsNone(atticus.fast('daily mode'))
            self.assertIsNone(atticus.fast('open Spotify'))
            run.assert_not_called()

    def test_risky_saved_shortcut_defers_to_safe_gate(self):
        with patch.object(atticus, 'skills', return_value={'send mail': {'cmds': ['osascript -e \'tell application "Mail" to send outgoing message 1\''], 'reply': 'Sent'}}), \
             patch.object(atticus.subprocess, 'run') as run:
            self.assertIsNone(atticus.replay('send mail'))
            run.assert_not_called()

    def test_fast_action_is_logged_once_at_zero_cost(self):
        with tempfile.TemporaryDirectory() as root, patch.object(atticus, 'DIR', root), \
             patch.multiple(atticus, licensed=lambda: True, ping=lambda: None,
                            fast=lambda _: 'Opened Spotify.', remember_turn=lambda *_: None), \
             patch.object(atticus.os, 'setpgrp'), patch.object(atticus.signal, 'signal'), \
             patch('sys.argv', ['atticus.py', '--fast', 'open Spotify']), \
             patch('sys.stdout', io.StringIO()), patch.dict(os.environ, {'ATTICUS_LIVE': '1'}):
            atticus.main()
            rows = atticus.read_rows(str(Path(root) / 'log.jsonl'))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['usd'], 0)
            self.assertTrue(rows[0]['live'])

    def test_confirmed_instant_command_skips_context_work(self):
        with patch.multiple(atticus, **{name: unittest.mock.DEFAULT for name in
                            ['start_claude', 'licensed', 'ping', 'fast', 'remember_turn', 'say', 'front', 'stop_claude']}) as mocks, \
             patch.object(atticus.threading, 'Thread'), patch.object(atticus.os, 'setpgrp'), \
             patch.object(atticus.signal, 'signal'), patch('sys.argv', ['atticus.py', '--wait']), \
             patch('sys.stdin', io.StringIO('Notes\topen Notes\n')), patch.dict(os.environ, {'ATTICUS_COMMIT': ''}):
            # Even without a CLI, --wait must read the request instead of treating its flag as a question.
            mocks['start_claude'].return_value = None
            mocks['licensed'].return_value = True
            mocks['fast'].return_value = 'Opened Notes.'
            atticus.main()
            mocks['fast'].assert_called_once_with('open Notes')
            mocks['front'].assert_not_called()
            mocks['say'].assert_called_once_with('✓', 'Opened Notes.')

    def test_stream_requires_a_real_terminal_answer(self):
        for stream, expected in [([], 'No answer came back. Please try again.'),
                                 ([{'type': 'result', 'result': ''}], 'No answer came back. Please try again.'),
                                 ([{'type': 'result', 'result': 'Found it.', 'total_cost_usd': .001}], 'Found it.')]:
            p = unittest.mock.Mock()
            p.stdin = io.StringIO()
            p.stdout = [json.dumps(row) + '\n' for row in stream]
            p.ask = ''; p.returncode = 0
            reply, _, _ = atticus.run('test', p)
            self.assertEqual(reply, expected)
            self.assertEqual(atticus.run.cmds, [])
            self.assertEqual(atticus.run.ok, expected == 'Found it.')

    def test_truncated_history_preserves_valid_turns(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'recent.jsonl'
            turn = {'t': time.time(), 'q': 'Hi', 'a': 'Hello'}
            path.write_text(json.dumps(turn) + '\nnull\n{"q":"cut off\n{}\n')
            with patch.object(atticus, 'RECENT', str(path)):
                self.assertEqual(atticus.recent(), [turn])

    def test_error_result_is_not_learned(self):
        p = unittest.mock.Mock()
        p.stdin = io.StringIO(); p.ask = ''; p.returncode = 0
        p.stdout = map(json.dumps, [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'open -a Notes'}}]}},
            {'type': 'result', 'is_error': True, 'result': 'Could not finish.'}])
        with patch.object(atticus, 'say'):
            atticus.run('test', p)
        self.assertEqual(atticus.run.cmds, [])
        self.assertEqual(atticus.run.ran, ['open -a Notes'])
        self.assertFalse(atticus.run.ok)

    def test_recovered_tool_error_keeps_success_but_is_not_learned(self):
        p = unittest.mock.Mock()
        p.stdin = io.StringIO(); p.ask = ''; p.returncode = 0
        p.stdout = map(json.dumps, [
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True}]}},
            {'type': 'result', 'result': 'Recovered.'}])
        atticus.run('test', p)
        self.assertTrue(atticus.run.ok)
        self.assertEqual(atticus.run.cmds, [])

    def test_cancellation_stops_descendant(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'child-survived'
            ready = Path(root) / 'ready'
            child = f'import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(0.5); Path({str(marker)!r}).touch()'
            parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
            p = subprocess.Popen([sys.executable, '-c', parent], start_new_session=True)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                atticus.stop_claude(p)
                p.wait(timeout=3)
                time.sleep(.6)
                self.assertFalse(marker.exists())
            finally:
                atticus.stop_claude(p)
                p.wait(timeout=3)

    def test_fast_worker_sigterm_stops_its_action_group(self):
        with tempfile.TemporaryDirectory() as root:
            marker, ready = Path(root) / 'action', Path(root) / 'ready'
            action = f'import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(.5); Path({str(marker)!r}).touch()'
            script = f'''import atticus, subprocess, sys, time
atticus.ping = lambda: None
atticus.licensed = lambda: True
def fast(_):
    subprocess.Popen([sys.executable, '-c', {action!r}])
    time.sleep(30)
atticus.fast = fast
sys.argv = ['atticus.py', '--fast', 'test']
atticus.main()
'''
            p = subprocess.Popen([sys.executable, '-c', script], env={**os.environ, 'ATTICUS_DIR': root})
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                p.terminate(); p.wait(timeout=3); time.sleep(.6)
                self.assertFalse(marker.exists())
            finally:
                if p.poll() is None:
                    p.terminate(); p.wait(timeout=3)

    def test_cli_stderr_cannot_block_the_stream(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / 'claude'
            fake.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("x" * 1000000)\nprint(\'{json.dumps({"type": "result", "result": "Ready."})}\')\n')
            fake.chmod(0o755)
            previous_handler = signal.getsignal(signal.SIGTERM)
            try:
                with patch.object(atticus.shutil, 'which', return_value=str(fake)), \
                     patch.dict(os.environ, {'PATH': root + os.pathsep + os.environ['PATH'], 'ATTICUS_COMMIT': ''}), \
                     patch.object(atticus, 'pref', side_effect=lambda k, d: False if k == 'safe' else d):
                    p = atticus.start_claude()
                    timer = threading.Timer(3, atticus.stop_claude, args=(p,))
                    timer.start()
                    try:
                        self.assertEqual(atticus.run('test', p)[0], 'Ready.')
                    finally:
                        timer.cancel(); timer.join(); atticus.stop_claude(p); p.errors.close()
            finally:
                signal.signal(signal.SIGTERM, previous_handler)


class WatchAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.notes = []
        for p in (patch.object(atticus.time, 'sleep'), patch.object(atticus, 'notify', side_effect=self.notes.append)):
            p.start()
            self.addCleanup(p.stop)

    def fake_app(self, out, code=0):
        f = tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False)
        f.write(f'#!/bin/sh\nprintf %s "{out}"\nexit {code}\n')
        f.close()
        os.chmod(f.name, 0o755)
        self.addCleanup(os.remove, f.name)
        return patch.object(atticus, 'APP', f.name)

    def test_cmd_watch_fires_after_two_passing_checks(self):
        atticus.watch('true', '')
        self.assertEqual(len(self.notes), 1)
        self.assertIn('done', self.notes[0])

    def test_phrase_gone_fires_and_present_waits(self):
        with self.fake_app('All set. Ready for the next task'):
            atticus.watch('', '', 'Orca', '', 'gone', 'esc to  interrupt')
        self.assertIn('Orca: done.', self.notes[-1])
        with self.fake_app('Working (12s - esc to interrupt)'), patch.object(atticus.time, 'time', side_effect=[0, 0, 1, 2, 1e9]):
            atticus.watch('', '', 'Orca', '', 'gone', 'esc to interrupt')
        self.assertIn('3 hours', self.notes[-1])

    def test_unreadable_window_stops_with_a_reason(self):
        with self.fake_app('', 1):
            atticus.watch('', '', 'Orca', '', 'shows', 'done')
        self.assertIn("can't see its window", self.notes[-1])

    def test_pick_prints_the_choice_the_box_sends_back(self):
        for answer, printed in [('1', 'b.pdf'), ('', 'None of these'), (None, 'The user did not pick')]:   # None: no answer in time
            with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user', return_value=answer) as ask, \
                 patch('sys.stdout', new_callable=io.StringIO) as out:
                atticus.pick('Which one?', ['a.pdf', 'b.pdf'])
            self.assertEqual(ask.call_args.args[0]['options'], ['a.pdf', 'b.pdf'])
            self.assertTrue(out.getvalue().startswith(printed))
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.PICK} "Which?" "a" "b"'}), 'Asking you which one')
        self.assertFalse(atticus.risky(f'{atticus.PICK} "Which file?" "~/delete-me.txt" "~/send.pdf"'))

    def test_set_flips_a_switch_by_name_never_safe_mode(self):
        self.assertIn('askMe', atticus.switches())
        self.assertFalse({'safe', 'paused'} & set(atticus.switches()))
        for words, key in [('the asking setting', 'askMe'), ('read answers aloud', 'speak'), ('safe mode', None)]:
            with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user') as ask, patch('sys.stdout', new_callable=io.StringIO):
                atticus.set_switch(words, 'off')
            self.assertEqual(ask.call_args.args[0] if ask.called else None, key and {'set': key, 'value': False})

    def test_unknown_command_never_shows_raw_shell(self):
        self.assertEqual(atticus.label('Bash', {'command': 'cat > /private/var/folders/x/prompt.txt <<EOF'}), 'Working on it')

    def test_prompt_stays_under_budget(self):
        # Every ask pays for this prompt. Raising the cap is a decision: check `atticus.py --budget` first.
        full = atticus.SYSTEM + atticus.ASK_ME + atticus.POINTER + atticus.LEARN_ME + atticus.TEACH + atticus.ASK_FIRST
        self.assertLessEqual(len(full), 10000)


if __name__ == '__main__':
    unittest.main()
