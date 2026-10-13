"""Daily capture stays anonymous and retries failed sends, without network access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import atticus


class TelemetryTests(unittest.TestCase):
    def test_success_is_daily_and_failure_can_retry(self):
        with tempfile.TemporaryDirectory(prefix='atticus telemetry ') as root:
            root = Path(root)
            curl = root / 'curl'
            curl.write_text('#!/bin/sh\nexit "${FAKE_CURL_STATUS:-0}"\n')
            curl.chmod(0o700)
            spawn = subprocess.Popen

            def fake_send(args, **kwargs):
                payload = json.loads(args[4])
                self.assertEqual(payload['event'], 'used')
                self.assertEqual(payload['api_key'], 'fixture-token')
                self.assertEqual(payload['properties'], {'$process_person_profile': False})
                self.assertEqual(set(payload), {'api_key', 'event', 'distinct_id', 'properties'})
                worker = spawn(args, **kwargs)
                worker.wait(timeout=5)
                return worker

            with patch.object(atticus, 'DIR', str(root)), patch.object(atticus, 'POSTHOG_KEY', 'fixture-token'), \
                 patch.object(atticus, 'pref', return_value=True), patch.object(atticus.subprocess, 'Popen', side_effect=fake_send) as send, \
                 patch.dict(os.environ, {'PATH': str(root), 'FAKE_CURL_STATUS': '22'}):
                atticus.ping()
                self.assertFalse((root / 'pinged').exists())
                identity = (root / 'id').read_text()
                os.environ['FAKE_CURL_STATUS'] = '0'
                atticus.ping()
                self.assertTrue((root / 'pinged').exists())
                atticus.ping()
                self.assertEqual(send.call_count, 2)
                self.assertEqual((root / 'id').read_text(), identity)

    def test_disabled_does_not_send(self):
        with tempfile.TemporaryDirectory() as root, patch.object(atticus, 'DIR', root), \
             patch.object(atticus.subprocess, 'Popen') as send:
            with patch.object(atticus, 'POSTHOG_KEY', ''):
                atticus.ping()
            with patch.object(atticus, 'POSTHOG_KEY', 'fixture-token'), patch.object(atticus, 'pref', return_value=False):
                atticus.ping()
            send.assert_not_called()
            self.assertEqual(list(Path(root).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
