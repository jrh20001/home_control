"""Tests for update_crontab.py (at-job generation)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
import datetime
import os
import subprocess
import sys
import unittest
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# suntime must be mocked before importing the module under test
sys.modules['suntime'] = MagicMock()
sys.modules['suntime'].Sun = MagicMock()
sys.modules['suntime'].SunTimeException = Exception

import update_crontab


class TestUpdateCrontab(unittest.TestCase):
    def setUp(self):
        self.mock_config = {
            "location": {"latitude": 37.0, "longitude": -122.0},
            "devices": {"a1": {"name": "Kitchen"}},
            "schedule": [
                {"time": "today", "offset": "23:59:00", "device": "a1", "action": "on"},
                {"time": "sunset", "offset": "-00:30:00", "device": "a1", "action": "off"},
            ],
        }
        self.config_patcher = patch('update_crontab.load_config',
                                    return_value=self.mock_config)
        self.config_patcher.start()

        self.calls = []

        def fake_run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if cmd[0] == 'atq':
                return CompletedProcess(cmd, 0, stdout=(
                    '101\tTue Oct 27 23:59:00 2026 a ubuntu\n'
                    '102\tTue Oct 27 23:58:00 2026 a ubuntu\n'), stderr='')
            if cmd[0] == 'at' and cmd[1] == '-c':
                body = ('sleep 0; echo rf a1 on | nc -N localhost 1099'
                        if cmd[2] == '101' else '/usr/local/bin/backup.sh')
                return CompletedProcess(cmd, 0, stdout=body, stderr='')
            return CompletedProcess(cmd, 0, stdout='', stderr='')

        self.subprocess_patcher = patch('subprocess.run', side_effect=fake_run)
        self.subprocess_patcher.start()

        self.sun = update_crontab.Sun.return_value
        self.sun.get_local_sunrise_time.return_value = datetime.datetime.now()
        self.sun.get_local_sunset_time.return_value = datetime.datetime.now()

    def tearDown(self):
        self.config_patcher.stop()
        self.subprocess_patcher.stop()

    def cmd_calls(self, name):
        return [c for c in self.calls if c[0][0] == name]

    def test_get_time_offset(self):
        self.assertEqual(update_crontab.get_time_offset("10:00:00"), (10, 0, 0))
        self.assertEqual(update_crontab.get_time_offset("-00:30:00"), (-0, -30, 0))

    def test_only_home_control_jobs_are_removed(self):
        update_crontab.main()
        atrm_calls = [c[0] for c in self.cmd_calls('atrm')]
        self.assertEqual(atrm_calls, [['atrm', '101']])   # 102 (unrelated) kept
        # every queued job was inspected
        inspected = sorted(c[0][2] for c in self.cmd_calls('at') if c[0][1] == '-c')
        self.assertEqual(inspected, ['101', '102'])

    def test_schedules_every_valid_entry(self):
        update_crontab.main()
        at_calls = [c for c in self.cmd_calls('at') if c[0][1] != '-c']
        self.assertEqual(len(at_calls), 2)
        for cmd, kwargs in at_calls:
            self.assertEqual(len(cmd), 3)                 # ['at', HH:MM, YYYY-MM-DD]
            self.assertRegex(cmd[1], r'^\d{2}:\d{2}$')
            self.assertRegex(cmd[2], r'^\d{4}-\d{2}-\d{2}$')
            self.assertIn('echo rf a1', kwargs['input'].decode())
            self.assertTrue(kwargs.get('check'))
            self.assertTrue(kwargs.get('timeout'))

    def test_past_today_entry_moves_to_tomorrow(self):
        now = datetime.datetime.now()
        past = now - datetime.timedelta(minutes=2)
        if past.date() != now.date():
            self.skipTest('too close to midnight for this check')
        self.mock_config['schedule'] = [{
            "time": "today",
            "offset": f"{past.hour:02d}:{past.minute:02d}:00",
            "device": "a1",
            "action": "on",
        }]
        update_crontab.main()
        at_calls = [c[0] for c in self.cmd_calls('at') if c[0][1] != '-c']
        self.assertEqual(len(at_calls), 1)
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(at_calls[0][2], tomorrow)

    def test_unknown_device_and_bad_offset_are_skipped(self):
        self.mock_config['schedule'] = [
            {"time": "today", "offset": "23:59:00", "device": "nope", "action": "on"},
            {"time": "today", "offset": "notatime", "device": "a1", "action": "on"},
            {"time": "someday", "offset": "23:59:00", "device": "a1", "action": "on"},
        ]
        update_crontab.main()
        self.assertEqual([c for c in self.cmd_calls('at') if c[0][1] != '-c'], [])

    def test_failed_at_call_is_reported(self):
        def failing_run(cmd, **kwargs):
            if cmd[0] == 'at' and cmd[1] != '-c':
                raise subprocess.CalledProcessError(1, cmd, stderr=b'at: failed')
            return CompletedProcess(cmd, 0, stdout='', stderr='')

        with patch('subprocess.run', side_effect=failing_run):
            rc = update_crontab.main()
        self.assertEqual(rc, 1)          # non-zero so callers can see the failure


if __name__ == '__main__':
    unittest.main(verbosity=2)
