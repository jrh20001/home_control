"""Tests for the Lights A (/config_lights_a) schedule editor.

Run from a writeable scratch copy of the app directory (python writes
hc_server.log into the cwd):

    cd /tmp/hctest && python3 -m unittest test_config_lights_a -v

The config.json used by each test is a private temp file.
"""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hc_server import HomeControlServer
from test_config_lights import form_from_html

FIXTURE = {
    "mqtt": {
        "broker": "hcontrol.local",
        "port": 1883,
        "topic_cmnd": "cmnd/tasmota_sprinklers/",
        "topic_stat": "stat/tasmota_sprinklers/RESULT",
    },
    "location": {"latitude": 37.37006, "longitude": -122.08839},
    "devices": {
        "a1": {"name": "Kitchen", "state": "off", "group": "1", "swno": "1", "dimr": "0"},
        "a5": {"name": "Family", "state": "off", "group": "1", "swno": "1", "dimr": "0"},
        "a15": {"name": "Family south", "state": "off", "group": "1", "swno": "1", "dimr": "0"},
        "lamp": {"name": "Lamp", "state": "off", "group": "2", "swno": "1", "dimr": "0"},
        "sprink": {"name": "Sprinkler", "state": "off", "group": "3", "swno": "1", "dimr": "0"},
    },
    "schedule": [
        {"time": "sunset", "offset": "00:00:00", "device": "a1", "action": "on"},
        {"time": "sunset", "offset": "00:00:40", "device": "a5", "action": "on"},
        {"time": "today", "offset": "22:29:00", "device": "a1", "action": "off"},
        {"time": "today", "offset": "23:59:20", "device": "a15", "action": "off"},
        {"time": "tomorrow", "offset": "02:00:00", "device": "a5", "action": "off"},
        {"time": "sunrise", "offset": "-00:30:00", "device": "a5", "action": "on"},
        {"time": "sunrise", "offset": "+00:30:00", "device": "a5", "action": "off"},
        {"time": "today", "offset": "12:00:00", "device": "a99", "action": "on"},
    ],
    "custom_key": {"keep": "me"},
}


class TestLightsASchedule(unittest.TestCase):
    def setUp(self):
        self.patcher = patch('paho.mqtt.client.Client')
        self.patcher.start()
        self.server = HomeControlServer()
        self.server.app.config['TESTING'] = True
        self.client = self.server.app.test_client()

        self.tmpdir = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmpdir, 'config.json')
        with open(self.conf, 'w') as f:
            json.dump(copy.deepcopy(FIXTURE), f, indent=4)
            f.write('\n')
        os.chmod(self.conf, 0o664)
        self.server.config_path = self.conf
        self.server.load_config()

    def tearDown(self):
        self.patcher.stop()

    # helpers -----------------------------------------------------------
    def get_html(self):
        return self.client.get('/config_lights_a').get_data(as_text=True)

    def page_form(self, **overrides):
        form = form_from_html(self.get_html())
        for key, value in overrides.items():
            if value is None:
                form.pop(key, None)
            else:
                form[key] = value
        return form

    def post(self, form, at_stdout='13587\tTue Oct 27 23:59:00 2026 a ubuntu\n'
                                  '13588\tTue Oct 27 22:29:00 2026 a ubuntu\n'):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return CompletedProcess(cmd, 0, stdout=at_stdout, stderr='')

        with patch('hc_server.subprocess.run', side_effect=fake_run):
            response = self.client.post('/config_lights_a', data=form)
        return response, calls

    def saved_config(self):
        with open(self.conf) as f:
            return json.load(f)

    # tests -------------------------------------------------------------
    def test_page_renders_every_entry_grouped_by_light(self):
        html = self.get_html()
        indices = re.findall(r'name="sched_(\d+)_action"', html)
        # the a99 entry belongs to no group-1 device: kept, but not editable here
        self.assertEqual(sorted(indices), [str(i) for i in range(7)])
        self.assertNotIn('sched_7_action', html)
        self.assertIn('Kitchen', html)
        self.assertIn('Family south', html)
        self.assertIn('add:', html)
        # a5 owns four of the entries
        self.assertEqual(html.count('name="sched_1_base"'), 1)
        self.assertIn('next ', html)          # computed next-run column
        self.assertIn('sunrise ', html)       # header sun times
        self.assertIn('config.json', html)

    def test_resolve_run_matches_update_crontab(self):
        sunrise = datetime(2026, 10, 27, 7, 30)
        sunset = datetime(2026, 10, 27, 18, 20)
        self.assertEqual(self.server.resolve_run('sunset', '00:00:40', sunrise, sunset),
                         ('Wed 18:20', 40))              # sunset +1 day, seconds = stagger
        self.assertEqual(self.server.resolve_run('sunset', '-00:30:00', sunrise, sunset),
                         ('Wed 17:50', 0))
        self.assertEqual(self.server.resolve_run('today', '22:29:10', sunrise, sunset),
                         ('Tue 22:29', 10))
        self.assertEqual(self.server.resolve_run('tomorrow', '02:00:00', sunrise, sunset),
                         ('Wed 02:00', 0))
        self.assertEqual(self.server.resolve_run('nonsense', '00:00:00', sunrise, sunset),
                         (None, 0))

    def test_unchanged_save_rewrites_identical_config(self):
        self.server.devices['a1']['state'] = 'on'    # runtime state, not in file
        form = self.page_form()
        response, calls = self.post(form)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved_config(), FIXTURE)
        self.assertEqual(self.server.devices['a1']['state'], 'on')   # state kept
        self.assertIn('at jobs queued', response.get_data(as_text=True))

        backups = [f for f in os.listdir(self.tmpdir) if f.startswith('config.json.bak.')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(os.stat(self.conf).st_mode & 0o777, 0o664)

        # one update_crontab run (as ubuntu) + one atq
        self.assertEqual(len(calls), 2)
        update_cmd, update_kw = calls[0]
        self.assertTrue(update_cmd[-1].endswith('update_crontab.py'))
        if os.geteuid() == 0:
            self.assertEqual(update_kw.get('user'), 'ubuntu')   # drop privileges
        else:
            self.assertNotIn('user', update_kw)
        self.assertEqual(calls[1][0], ['atq'])

    def test_edit_add_and_delete(self):
        form = self.page_form(
            sched_0_offset='00:02:00',       # a1 on: sunset +2 min
            sched_2_delete='on',             # drop a1 off
            new_a15_action='on',
            new_a15_base='sunset',
            new_a15_sign='+',
            new_a15_offset='00:00:00',       # add Family south on
        )
        response, calls = self.post(form)
        self.assertEqual(response.status_code, 200)

        schedule = self.saved_config()['schedule']
        self.assertEqual(len(schedule), len(FIXTURE['schedule']))   # -1 +1
        self.assertEqual(schedule[0], {"time": "sunset", "offset": "00:02:00",
                                       "device": "a1", "action": "on"})
        self.assertFalse([e for e in schedule
                          if e['device'] == 'a1' and e['action'] == 'off'])
        self.assertEqual(schedule[-1], {"time": "sunset", "offset": "00:00:00",
                                        "device": "a15", "action": "on"})
        # unrelated device entry + other config blocks untouched
        self.assertEqual([e for e in schedule if e['device'] == 'a99'],
                         [{"time": "today", "offset": "12:00:00",
                           "device": "a99", "action": "on"}])
        self.assertEqual(self.saved_config()['custom_key'], {'keep': 'me'})
        self.assertEqual(self.saved_config()['location'], FIXTURE['location'])
        self.assertEqual(len(calls), 2)

    def test_negative_sign_and_normalisation(self):
        form = self.page_form(sched_6_sign='+', sched_6_offset='00:00:05',
                              sched_5_sign='-', sched_5_offset='01:00:00')
        self.post(form)
        schedule = self.saved_config()['schedule']
        self.assertEqual(schedule[6]['offset'], '00:00:05')
        self.assertEqual(schedule[5]['offset'], '-01:00:00')

    def test_invalid_offset_is_rejected_without_writing(self):
        with open(self.conf, 'rb') as f:
            before = f.read()
        form = self.page_form(sched_0_offset='nonsense')
        response, calls = self.post(form)

        self.assertEqual(response.status_code, 400)
        self.assertIn('Not saved', response.get_data(as_text=True))
        with open(self.conf, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(calls, [])
        self.assertFalse([f for f in os.listdir(self.tmpdir)
                          if f.startswith('config.json.bak.')])

    def test_blank_add_row_is_ignored(self):
        form = self.page_form(new_a1_offset='', new_a15_offset='')
        response, calls = self.post(form)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved_config(), FIXTURE)
        self.assertEqual(len(calls), 2)

    @patch('subprocess.run')
    def test_home_page_shows_config_button(self, _sp):
        html = self.client.get('/a1/off').get_data(as_text=True)
        self.assertIn('/config_lights_a', html)
        self.assertIn('Configuration', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
