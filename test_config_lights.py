"""Tests for the Lights B (/config_lights) page.

Run from a writeable scratch copy of the app directory (python writes
hc_server.log into the cwd), with templates/ and static/ alongside:

    cd /tmp/hctest && python3 -m unittest test_config_lights -v

The fixture devices are injected, so no particular config.json is required.
"""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
import json
import os
import re
import sys
import threading
import time
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hc_server import HomeControlServer

# Captured shapes: lamp_one uses absolute times, lamp_two uses a sunset-relative
# ON timer (Mode 2 + negative offset), like the real garage-side / cabinet lights.
LAMP_ONE = {
    "Timers": "ON",
    "Timer1": {"Enable": 1, "Mode": 0, "Time": "06:30", "Window": 0,
               "Days": "1111111", "Repeat": 1, "Output": 1, "Action": 1},
    "Timer2": {"Enable": 1, "Mode": 0, "Time": "23:30", "Window": 0,
               "Days": "1111111", "Repeat": 1, "Output": 1, "Action": 0},
    "Timer3": {"Enable": 0, "Mode": 0, "Time": "00:00", "Window": 0,
               "Days": "0000000", "Repeat": 0, "Output": 1, "Action": 0},
    "Timer4": {"Enable": 0, "Mode": 0, "Time": "00:00", "Window": 0,
               "Days": "0000000", "Repeat": 0, "Output": 1, "Action": 0},
}
LAMP_TWO = {
    "Timers": "ON",
    "Timer1": {"Enable": 1, "Mode": 2, "Time": "-00:15", "Window": 10,
               "Days": "1010101", "Repeat": 1, "Output": 1, "Action": 1},
    "Timer2": {"Enable": 0, "Mode": 0, "Time": "22:30", "Window": 0,
               "Days": "1111111", "Repeat": 1, "Output": 1, "Action": 0},
    "Timer3": {"Enable": 0, "Mode": 0, "Time": "00:00", "Window": 0,
               "Days": "0000000", "Repeat": 0, "Output": 1, "Action": 0},
    "Timer4": {"Enable": 0, "Mode": 0, "Time": "00:00", "Window": 0,
               "Days": "0000000", "Repeat": 0, "Output": 1, "Action": 0},
}
IPS = {"lamp_one": "192.168.50.47", "lamp_two": "192.168.50.101"}
PAYLOADS = {"lamp_one": LAMP_ONE, "lamp_two": LAMP_TWO}
MIN_CMD_PARTS = 3


class FormParser(HTMLParser):
    """Rebuild what the browser would submit from the rendered page."""

    def __init__(self):
        super().__init__()
        self.fields = {}
        self._select = None
        self._options = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'input':
            if 'disabled' in a or not a.get('name'):
                return
            if a.get('type') == 'checkbox':
                if 'checked' in a:
                    self.fields[a['name']] = a.get('value', 'on')
            else:
                self.fields[a['name']] = a.get('value', '')
        elif tag == 'select' and a.get('name'):
            self._select = a['name']
            self._options = []
        elif tag == 'option' and self._select:
            self._options.append((a.get('value', ''), 'selected' in a))

    def handle_endtag(self, tag):
        if tag == 'select' and self._select:
            chosen = [v for v, sel in self._options if sel]
            value = chosen[0] if chosen else (self._options[0][0] if self._options else '')
            self.fields[self._select] = value
            self._select, self._options = None, []


def form_from_html(html):
    parser = FormParser()
    parser.feed(html)
    return parser.fields


class TestLightsConfig(unittest.TestCase):
    def setUp(self):
        self.patcher = patch('paho.mqtt.client.Client')
        self.patcher.start()
        self.server = HomeControlServer()
        self.server.devices = {
            'lamp_one': {'name': 'Lamp one', 'state': 'off', 'group': '2', 'swno': '1', 'dimr': '0'},
            'lamp_two': {'name': 'Lamp two', 'state': 'off', 'group': '2', 'swno': '1', 'dimr': '0'},
            'sprink': {'name': 'Sprinkler test', 'state': 'off', 'group': '3', 'swno': '1', 'dimr': '0'},
        }
        self.server.app.config['TESTING'] = True
        self.server.light_wait_seconds = 1.0
        self.server.light_ip_wait_seconds = 1.0
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.patcher.stop()

    # helpers -----------------------------------------------------------
    def deliver(self, topic, payload):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.server.on_message(None, None, msg)

    def fake_device(self, dev, timers=None):
        if timers is not None:
            self.deliver(f'stat/tasmota_{dev}/RESULT', timers)
        self.deliver(f'stat/tasmota_{dev}/STATUS5', {"StatusNET": {"IPAddress": IPS[dev]}})

    def publish_hook(self, sent, saved):
        """Simulate the devices answering Timers / Status / TimerN commands."""
        def publish(topic, payload=None):
            sent.append((topic, payload))
            parts = topic.split('/')
            if len(parts) < MIN_CMD_PARTS or parts[0] != 'cmnd':
                return MagicMock()
            dev = parts[1][len('tasmota_'):]
            if self.server.light_lwt.get(dev) == 'Offline':
                return MagicMock()          # offline devices never answer
            cmd = parts[2]
            if cmd == 'Timers' and payload == '':
                if dev in PAYLOADS:
                    self.fake_device(dev, PAYLOADS[dev])
            elif cmd == 'Status':
                self.fake_device(dev)
            elif cmd in ('Timer1', 'Timer2'):
                saved[f'{cmd}_{dev}'] = json.loads(payload)
                self.deliver(f'stat/tasmota_{dev}/RESULT', {cmd: json.loads(payload)})
            return MagicMock()
        return publish

    def page_form(self, **overrides):
        """GET the page, then build the form a browser would submit."""
        sent, saved = [], {}
        with patch.object(self.server.mqtt_client, 'publish',
                          side_effect=self.publish_hook(sent, saved)):
            html = self.client.get('/config_lights').get_data(as_text=True)
        form = form_from_html(html)
        for key, value in overrides.items():
            if value is None:
                form.pop(key, None)
            else:
                form[key] = value
        return form

    def post(self, form):
        sent, saved = [], {}
        with patch.object(self.server.mqtt_client, 'publish',
                          side_effect=self.publish_hook(sent, saved)):
            response = self.client.post('/config_lights', data=form)
        return response, sent, saved

    def day_states(self, html, dev):
        marks = []
        for j in range(7):
            m = re.search(rf'id="days_{dev}{j}"[^>]*>', html, re.DOTALL)
            marks.append('X' if m and 'checked' in m.group(0) else '.')
        return ''.join(marks)

    # tests -------------------------------------------------------------
    @patch('subprocess.run')
    def test_home_shows_config_button_for_group2(self, _sp):
        html = self.client.get('/lamp_one/off').get_data(as_text=True)
        self.assertIn('/config_lights', html)
        self.assertIn('Configuration', html)

    @patch('subprocess.run')
    def test_sprinkler_page_keeps_its_config_button(self, _sp):
        html = self.client.get('/sprink/off').get_data(as_text=True)
        self.assertIn('href="/config"', html)
        self.assertNotIn('/config_lights', html)

    def test_rows_render_device_values(self):
        sent, saved = [], {}
        with patch.object(self.server.mqtt_client, 'publish',
                          side_effect=self.publish_hook(sent, saved)):
            html = self.client.get('/config_lights').get_data(as_text=True)

        self.assertIn('Lamp one', html)
        self.assertIn('tasmota_lamp_one', html)
        self.assertIn('192.168.50.47', html)
        self.assertIn('name="ontime_lamp_one" value="06:30"', html)
        self.assertIn('name="offtime_lamp_one" value="23:30"', html)
        self.assertIn('runs 1020 min', html)
        self.assertNotIn('unreachable', html)

        # lamp_two: sunset mode + negative offset preserved, OFF timer disabled,
        # Window preserved, no duration because ON is not an absolute time
        self.assertIn('<option value="2" selected>Sunset</option>', html)
        self.assertIn('<option value="-" selected>', html)
        self.assertIn('name="ontime_lamp_two" value="00:15"', html)
        self.assertIn('name="onwindow_lamp_two" value="10"', html)
        self.assertRegex(html, r'id="offen_lamp_two" name="offen_lamp_two"\s*>')
        self.assertEqual(self.day_states(html, 'lamp_two'), 'X.X.X.X')
        self.assertEqual(self.day_states(html, 'lamp_one'), 'XXXXXXX')
        self.assertNotIn('runs', html.split('Lamp two')[1].split('</div>')[0])

        topics = [t for t, _ in sent]
        self.assertIn('cmnd/tasmota_lamp_one/Timers', topics)
        self.assertIn('cmnd/tasmota_lamp_one/Status', topics)
        self.assertIn('cmnd/tasmota_lamp_two/Timers', topics)

    def test_post_preserves_mode_window_and_sends_enable(self):
        form = self.page_form(ontime_lamp_two='00:30',
                              offen_lamp_two=None,      # uncheck OFF enable
                              days_lamp_two2=None)      # uncheck Wednesday
        response, sent, saved = self.post(form)
        self.assertEqual(response.status_code, 200)

        t1 = saved['Timer1_lamp_two']
        self.assertEqual(t1['Enable'], 1)
        self.assertEqual(t1['Mode'], 2)                    # sunset kept
        self.assertEqual(t1['Time'], '-00:30')
        self.assertEqual(t1['Days'], '1000101')
        self.assertEqual(t1['Action'], 1)
        self.assertEqual(t1['Output'], 1)
        self.assertEqual(t1['Window'], 10)                 # preserved
        self.assertEqual(t1['Repeat'], 1)
        self.assertNotIn('Arm', t1)

        t2 = saved['Timer2_lamp_two']
        self.assertEqual(t2['Enable'], 0)
        self.assertEqual(t2['Action'], 0)
        self.assertEqual(t2['Time'], '22:30')

        self.assertEqual(saved['Timer1_lamp_one']['Time'], '06:30')
        cmnds = {t: p for t, p in sent if t.startswith('cmnd/')}
        self.assertEqual(cmnds['cmnd/tasmota_lamp_one/Timers'], 'ON')

    def test_master_off_and_unreachable_skipped(self):
        self.deliver('tele/tasmota_lamp_two/LWT', b'Offline')
        form = self.page_form()
        self.assertNotIn('ontime_lamp_two', form)          # refused row disabled
        form.pop('timers_lamp_one')                        # master off
        response, sent, _saved = self.post(form)

        self.assertEqual(response.status_code, 200)
        topics = [t for t, _ in sent]
        self.assertFalse([t for t in topics if 'lamp_two' in t], topics)
        self.assertIn('cmnd/tasmota_lamp_one/Timer1', topics)
        cmnds = {t: p for t, p in sent if t.startswith('cmnd/')}
        self.assertEqual(cmnds['cmnd/tasmota_lamp_one/Timers'], 'OFF')

    def test_unreachable_row_marked_and_disabled(self):
        self.deliver('tele/tasmota_lamp_two/LWT', b'Offline')
        sent, saved = [], {}
        with patch.object(self.server.mqtt_client, 'publish',
                          side_effect=self.publish_hook(sent, saved)):
            html = self.client.get('/config_lights').get_data(as_text=True)
        two_row = html.split('Lamp two')[1]
        self.assertIn('unreachable', two_row.split('</div>')[0])
        self.assertIn('disabled', two_row)
        self.assertNotIn('disabled', html.split('Lamp two')[0])   # lamp_one fine

    def test_get_waits_without_holding_the_lock(self):
        """The MQTT thread must be able to deliver while the page is waiting."""
        def delayed():
            time.sleep(0.05)
            for dev in ('lamp_one', 'lamp_two'):
                self.fake_device(dev, PAYLOADS[dev])

        worker = threading.Thread(target=delayed)
        worker.start()
        try:
            with patch.object(self.server.mqtt_client, 'publish'):
                html = self.client.get('/config_lights').get_data(as_text=True)
        finally:
            worker.join()
        self.assertNotIn('unreachable', html)
        self.assertIn('192.168.50.47', html)

    def test_invalid_time_rejects_the_whole_save(self):
        form = self.page_form(ontime_lamp_one='nonsense')
        response, sent, saved = self.post(form)
        self.assertEqual(response.status_code, 400)
        self.assertIn('invalid time or mode', response.get_data(as_text=True))
        self.assertEqual(sent, [])               # nothing published
        self.assertEqual(saved, {})              # no timers written


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestSubscriptions(unittest.TestCase):
    """Topic filters must be valid MQTT (paho raises on 'tasmota_+')."""

    def test_filters_are_valid_and_cover_lights(self):
        with patch('paho.mqtt.client.Client'):
            server = HomeControlServer()
        server.mqtt_client = MagicMock()
        server.on_connect(server.mqtt_client, None, None, 0)
        filters = [c.args[0] for c in server.mqtt_client.subscribe.call_args_list]
        for f in filters:
            for level in f.split('/'):
                if level in ('+', '#'):
                    continue
                assert '+' not in level and '#' not in level, f
        self.assertIn(server.MQTT_TOPIC_STAT, filters)
        self.assertIn('stat/+/RESULT', filters)
        self.assertIn('stat/+/STATUS5', filters)
        self.assertIn('tele/+/LWT', filters)
