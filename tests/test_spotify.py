"""Spotify actions use fake lookup, AppleScript, and accessibility layers only."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class SpotifyTests(unittest.TestCase):
    def test_lookup_prefers_cached_playlist_and_falls_back_to_web(self):
        cached = ('spotify:playlist:cached', 'Road trip')
        with patch.object(atticus, 'playlist', return_value=cached), patch.object(atticus, 'get') as get:
            self.assertEqual(atticus.spotify_item('road trip'), cached)
            get.assert_not_called()

        track = 'A' * 22
        page = f'https://open.spotify.com/track/{track}'
        with patch.object(atticus, 'playlist', return_value=None), \
             patch.object(atticus, 'get', side_effect=[page, json.dumps({'title': 'Fixture song'})]):
            self.assertEqual(atticus.spotify_item('fixture song'), (f'spotify:track:{track}', 'Fixture song'))

    def test_play_uses_shared_lookup_and_keeps_spotify_behind(self):
        item = ('spotify:track:' + 'B' * 22, 'Fixture song')
        with patch.object(atticus, 'spotify_item', return_value=item) as lookup, \
             patch.object(atticus, 'osa', side_effect=['Editor', '']) as osa, \
             patch.object(atticus, 'preview_app') as preview, patch.object(atticus, 'wants_focus', return_value=False), \
             patch.object(atticus, 'keep_behind') as behind:
            self.assertEqual(atticus.play('fixture'), 'Playing Fixture song on Spotify.')
        lookup.assert_called_once_with('fixture', True)
        self.assertIn(item[0], osa.call_args_list[1].args[0])
        preview.assert_called_once_with('Spotify', 'play')
        behind.assert_called_once_with('Editor', 'Spotify')

    def test_queue_uses_discovered_ax_labels_and_verifies_the_toast(self):
        item = ('spotify:track:' + 'C' * 22, 'Fixture song')
        page = {'texts': ['Fixture song'], 'nodes': [{'role': 'AXButton', 'label': 'More options for Fixture song'}]}
        menu = {'texts': [], 'nodes': [{'role': 'AXMenuItem', 'label': 'Add to queue'}]}
        toast = {'texts': ['Added to queue'], 'nodes': []}
        actions = [('Clicked More options', True), ('Clicked Add to queue\nAdded to queue', True)]
        with patch.object(atticus, 'spotify_item', return_value=item), \
             patch.object(atticus, 'osa', return_value='Editor'), \
             patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
             patch.object(atticus, 'preview_app'), patch.object(atticus, 'hands_map', return_value=page), \
             patch.object(atticus, 'cached_hands', side_effect=[menu, toast]), \
             patch.object(atticus, 'hands', side_effect=actions) as hands, \
             patch.object(atticus, 'keep_behind') as behind:
            self.assertEqual(atticus.queue('fixture'), 'Added Fixture song to your Spotify queue.')
        run.assert_called_once_with(['open', '-g', item[0]], capture_output=True)
        self.assertEqual(hands.call_args_list[0].args, ('click', 'More options for Fixture song', 'near', 'Fixture song'))
        self.assertEqual(hands.call_args_list[1].args, ('click', 'Add to queue'))
        self.assertEqual(behind.call_count, 3)

    def test_queue_can_verify_the_track_in_the_queue_view(self):
        item = ('spotify:track:' + 'D' * 22, 'Another fixture')
        page = {'texts': ['Another fixture'], 'nodes': [{'role': 'AXButton', 'label': 'More options'}]}
        menu = {'texts': [], 'nodes': [{'role': 'AXMenuItem', 'label': 'Add to queue'}]}
        queue_button = {'texts': [], 'nodes': [{'role': 'AXButton', 'label': 'Queue'}]}
        queue_view = {'texts': ['Another fixture'], 'nodes': []}
        with patch.object(atticus, 'spotify_item', return_value=item), patch.object(atticus, 'osa', return_value='Editor'), \
             patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(atticus, 'preview_app'), patch.object(atticus, 'hands_map', return_value=page), \
             patch.object(atticus, 'cached_hands', side_effect=[menu, queue_button, queue_view]), \
             patch.object(atticus, 'hands', side_effect=[('More', True), ('Added', True), ('Queue opened', True)]) as hands, \
             patch.object(atticus, 'keep_behind'):
            self.assertEqual(atticus.queue('another'), 'Added Another fixture to your Spotify queue.')
        self.assertEqual(hands.call_args_list[-1].args, ('click', 'Queue'))

    def test_queue_does_not_claim_success_when_spotify_exposes_no_verification(self):
        item = ('spotify:track:' + 'E' * 22, 'Unverified fixture')
        page = {'texts': ['Unverified fixture'], 'nodes': [{'role': 'AXButton', 'label': 'More options'}]}
        menu = {'texts': [], 'nodes': [{'role': 'AXMenuItem', 'label': 'Add to queue'}]}
        with patch.object(atticus, 'spotify_item', return_value=item), patch.object(atticus, 'osa', return_value='Editor'), \
             patch.object(atticus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(atticus, 'preview_app'), patch.object(atticus, 'hands_map', return_value=page), \
             patch.object(atticus, 'cached_hands', side_effect=[menu, {}]), \
             patch.object(atticus, 'hands', side_effect=[('More', True), ('Added', True)]) as hands, \
             patch.object(atticus, 'keep_behind'), patch.object(atticus.time, 'sleep'):
            reply = atticus.queue('unverified')
        self.assertIn('could not verify it', reply)
        self.assertIn('do not retry blindly', reply)
        self.assertEqual(hands.call_count, 2)

    def test_queue_explains_the_album_and_playlist_limit_without_opening_spotify(self):
        for kind in ('album', 'playlist', 'artist'):
            with self.subTest(kind=kind), patch.object(atticus, 'spotify_item', return_value=(f'spotify:{kind}:id', 'Fixture')), \
                 patch.object(atticus.subprocess, 'run') as run, patch.object(atticus, 'hands') as hands:
                reply = atticus.queue('fixture')
            self.assertIn(f'tracks in that {kind}', reply)
            self.assertIn('Name one song', reply)
            run.assert_not_called()
            hands.assert_not_called()

    def test_unambiguous_queue_requests_run_without_ai(self):
        with patch.object(atticus, 'pref', side_effect=lambda _, default: default), \
             patch.object(atticus, 'skills', return_value={}), patch.object(atticus, 'fast_hands', return_value=None), \
             patch.object(atticus, 'queue', return_value='Added Fixture song to your Spotify queue.') as queue, \
             patch.dict(atticus.os.environ, {'ATTICUS_LIVE': '1'}):
            for request in ('add Fixture song to my queue', 'queue Fixture song next'):
                with self.subTest(request=request):
                    self.assertEqual(atticus.fast(request), 'Added Fixture song to your Spotify queue.')
                    queue.assert_called_with('fixture song')
            queue.reset_mock()
            self.assertIsNone(atticus.fast('add it to my queue'))
            queue.assert_not_called()

    def test_queue_cli_and_step_label(self):
        with patch.object(atticus, 'queue', return_value='Added Fixture song to your Spotify queue.') as queue, \
             patch('sys.argv', ['atticus.py', '--queue', 'Fixture', 'song']), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.main()
        queue.assert_called_once_with('Fixture song')
        self.assertEqual(out.getvalue().strip(), 'Added Fixture song to your Spotify queue.')
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.QUEUE} "Fixture song"'}),
                         'Adding Fixture song to the queue')
        self.assertIn(atticus.QUEUE, atticus.SYSTEM)


if __name__ == '__main__':
    unittest.main()
