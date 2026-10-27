"""Tests for the sprinkler (/config) timer fetch + conversion.

Run from a writeable scratch copy of the app directory (python writes
hc_server.log into the cwd).
"""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
import json
import os
import sys
import threading
import time as _time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hc_server import HomeControlServer

# Real RESULT payload captured from stat/tasmota_sprinklers/RESULT
REAL = {"Timers":"ON","Timer1":{"Enable":1,"Mode":0,"Time":"23:30","Window":0,"Days":"1111111","Repeat":1,"Output":1,"Action":1},"Timer2":{"Enable":1,"Mode":0,"Time":"23:35","Window":0,"Days":"1111111","Repeat":1,"Output":1,"Action":0},"Timer3":{"Enable":1,"Mode":0,"Time":"23:20","Window":0,"Days":"1111111","Repeat":1,"Output":5,"Action":1},"Timer4":{"Enable":1,"Mode":0,"Time":"23:25","Window":0,"Days":"1111111","Repeat":1,"Output":5,"Action":0},"Timer5":{"Enable":1,"Mode":0,"Time":"12:30","Window":0,"Days":"1111111","Repeat":1,"Output":6,"Action":1},"Timer6":{"Enable":1,"Mode":0,"Time":"12:38","Window":0,"Days":"1111111","Repeat":1,"Output":6,"Action":0},"Timer7":{"Enable":1,"Mode":0,"Time":"23:10","Window":0,"Days":"1111111","Repeat":1,"Output":2,"Action":1},"Timer8":{"Enable":1,"Mode":0,"Time":"23:15","Window":0,"Days":"1111111","Repeat":1,"Output":2,"Action":0},"Timer9":{"Enable":1,"Mode":0,"Time":"23:00","Window":0,"Days":"1111111","Repeat":1,"Output":3,"Action":1},"Timer10":{"Enable":1,"Mode":0,"Time":"23:04","Window":0,"Days":"1111111","Repeat":1,"Output":3,"Action":0},"Timer11":{"Enable":1,"Mode":0,"Time":"12:40","Window":0,"Days":"1010101","Repeat":1,"Output":4,"Action":1},"Timer12":{"Enable":1,"Mode":0,"Time":"12:41","Window":0,"Days":"1111111","Repeat":1,"Output":4,"Action":0},"Timer13":{"Enable":0,"Mode":0,"Time":"00:00","Window":0,"Days":"0000000","Repeat":1,"Output":8,"Action":0},"Timer14":{"Enable":0,"Mode":0,"Time":"00:00","Window":0,"Days":"0000000","Repeat":0,"Output":8,"Action":0},"Timer15":{"Enable":0,"Mode":0,"Time":"00:00","Window":0,"Days":"0000000","Repeat":1,"Output":8,"Action":0},"Timer16":{"Enable":0,"Mode":0,"Time":"00:00","Window":0,"Days":"0000000","Repeat":0,"Output":8,"Action":0}}

class T(unittest.TestCase):
    def setUp(self):
        self.p = patch('paho.mqtt.client.Client')
        self.p.start()
        self.s = HomeControlServer()
        self.s.app.config['TESTING'] = True
        self.s.sprinkler_wait_seconds = 0.2
        self.s.mqtt_client.is_connected.return_value = True

    def tearDown(self):
        self.p.stop()

    def m(self, payload, topic='stat/tasmota_sprinklers/RESULT'):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = json.dumps(payload).encode()
        self.s.on_message(None, None, msg)

    def test_flat_payload_populates_and_marks_ready(self):
        self.m(REAL)
        self.assertTrue(self.s.mqtt_mess5)
        spr = self.s.sprinklers
        self.assertEqual(spr['Timers'], 'ON')
        self.assertEqual(spr['Sprinkler1']['Time'], '23:30')
        self.assertEqual(spr['Sprinkler1']['Duration'], 5)
        self.assertEqual(spr['Sprinkler1']['sprinkler_name'], 'Back East')
        self.assertEqual(spr['Sprinkler2']['sprinkler_name'], 'Back West')
        self.assertEqual(spr['Sprinkler3']['sprinkler_name'], 'Back Drip')
        self.assertEqual(spr['Sprinkler4']['Arm'], 1)
        self.assertEqual(spr['Sprinkler6']['Days'], '1010101')

    def test_get_config_renders_device_values(self):
        self.m(REAL)                       # prefill as if MQTT arrived
        c = self.s.app.test_client()
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.get('/config')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        pub.assert_called_with(self.s.MQTT_TOPIC_CMND + 'Timers', json.dumps(''))
        self.assertIn('value="23:30"', html)
        self.assertIn('Back East', html)
        self.assertNotIn('value="12:38"', html)  # end timer, not start
        self.assertIn('<option value="5" selected>5 min</option>', html)
        self.assertIn('<option value="1" selected>1 min</option>', html)

    def test_post_sends_enable_key(self):
        self.m(REAL)
        c = self.s.app.test_client()
        form = {'Timers': 'on'}
        rows = [('23:30', 5, '1111111'), ('23:20', 5, '1111111'),
                ('12:30', 8, '1111111'), ('23:10', 5, '1111111'),
                ('23:00', 4, '1111111'), ('12:40', 1, '1010101')]
        for i, (t, d, days) in enumerate(rows, start=1):
            form[f'Arm{i}'] = 'on'
            form[f'Time{i}'] = t
            form[f'Duration{i}'] = str(d)
            for j, ch in enumerate(days):
                if ch == '1':
                    form[f'Days{i}{j}'] = 'on'
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.post('/config', data=form)
        self.assertEqual(r.status_code, 200)
        sent = {call.args[0]: call.args[1] for call in pub.call_args_list}
        self.assertEqual(sent[self.s.MQTT_TOPIC_CMND + 'Timers'], 'ON')
        t1 = json.loads(sent[self.s.MQTT_TOPIC_CMND + 'Timer1'])
        t2 = json.loads(sent[self.s.MQTT_TOPIC_CMND + 'Timer2'])
        self.assertEqual(t1['Enable'], 1)
        self.assertNotIn('Arm', t1)
        self.assertEqual(t2['Time'], '23:35')
        self.assertEqual(t2['Enable'], 1)
        t12 = json.loads(sent[self.s.MQTT_TOPIC_CMND + 'Timer12'])
        self.assertEqual(t12['Time'], '12:41')

    def test_chunked_payload_still_supported(self):
        self.m({"Timers": "ON"})
        self.assertFalse(self.s.mqtt_mess5)
        for k in range(1, 5):
            chunk = {f"Timer{4 * (k - 1) + n}": REAL[f"Timer{4 * (k - 1) + n}"]
                     for n in range(1, 5)}
            self.m({f"Timers{k}": chunk})
        self.assertTrue(self.s.mqtt_mess5)
        self.assertEqual(self.s.sprinklers['Sprinkler1']['Time'], '23:30')

    # ---- validation / offline behaviour (audit fixes) -----------------
    def test_get_waits_without_holding_the_lock(self):
        def delayed():
            _time.sleep(0.05)
            self.m(REAL)

        worker = threading.Thread(target=delayed)
        worker.start()
        try:
            with patch.object(self.s.mqtt_client, 'publish'):
                r = self.s.app.test_client().get('/config')
        finally:
            worker.join()
        html = r.get_data(as_text=True)
        self.assertIn('value="23:30"', html)
        self.assertNotIn('did not answer', html)

    def test_get_warns_when_mqtt_is_down(self):
        self.m(REAL)
        self.s.mqtt_client.is_connected.return_value = False
        c = self.s.app.test_client()
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.get('/config')
        self.assertEqual(r.status_code, 200)
        self.assertIn('MQTT broker is not connected', r.get_data(as_text=True))
        pub.assert_not_called()          # no point publishing into a dead link

    def test_post_rejected_when_controller_state_unknown(self):
        self.s.sprinklers = {}
        self.s.timers = {}
        c = self.s.app.test_client()
        form = {'Timers': 'on'}
        for i in range(1, 7):
            form.update({f'Arm{i}': 'on', f'Time{i}': '06:00', f'Duration{i}': '5'})
            for j in range(7):
                form[f'Days{i}{j}'] = 'on'
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.post('/config', data=form)
        self.assertEqual(r.status_code, 400)
        self.assertIn('state unknown', r.get_data(as_text=True))
        pub.assert_not_called()

    def test_post_rejected_on_bad_duration_and_time(self):
        self.m(REAL)
        c = self.s.app.test_client()
        form = {'Timers': 'on'}
        for i in range(1, 7):
            form.update({f'Arm{i}': 'on', f'Time{i}': '06:00', f'Duration{i}': '5'})
            for j in range(7):
                form[f'Days{i}{j}'] = 'on'
        form['Duration2'] = '0'
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.post('/config', data=form)
        self.assertEqual(r.status_code, 400)
        self.assertIn('duration must be 1-60', r.get_data(as_text=True))
        pub.assert_not_called()

        form['Duration2'] = '5'
        form['Time3'] = '25:00'
        with patch.object(self.s.mqtt_client, 'publish') as pub:
            r = c.post('/config', data=form)
        self.assertEqual(r.status_code, 400)
        self.assertIn('invalid start time', r.get_data(as_text=True))
        pub.assert_not_called()

    def test_post_keeps_sprinkler_name_and_output(self):
        self.m(REAL)
        c = self.s.app.test_client()
        form = {'Timers': 'on'}
        for i in range(1, 7):
            form.update({f'Arm{i}': 'on', f'Time{i}': '06:00', f'Duration{i}': '7'})
            for j in range(7):
                form[f'Days{i}{j}'] = 'on'
        with patch.object(self.s.mqtt_client, 'publish'):
            r = c.post('/config', data=form)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Sprinkler timers saved', r.get_data(as_text=True))
        self.assertEqual(self.s.sprinklers['Sprinkler1']['sprinkler_name'], 'Back East')
        self.assertEqual(self.s.sprinklers['Sprinkler2']['Output'], 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
