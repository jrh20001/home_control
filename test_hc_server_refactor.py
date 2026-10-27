"""Route-level tests for hc_server.py (devices, actions)."""

# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hc_server import HomeControlServer

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
        "outdoor_side": {"name": "Garage side", "state": "off", "group": "2",
                         "swno": "1", "dimr": "0"},
        "spare8": {"name": "Spare8", "state": "off", "group": "3", "swno": "8", "dimr": "0"},
    },
    "schedule": [],
}


class TestHomeControlServer(unittest.TestCase):
    def setUp(self):
        self.mqtt_patcher = patch('paho.mqtt.client.Client')
        self.mqtt_patcher.start()
        self.server = HomeControlServer()
        self.server.app.config['TESTING'] = True
        self.server.command_timeout = 10

        self.tmpdir = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmpdir, 'config.json')
        with open(self.conf, 'w') as f:
            json.dump(copy.deepcopy(FIXTURE), f, indent=4)
            f.write('\n')
        self.server.config_path = self.conf
        self.server.load_config()
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.mqtt_patcher.stop()

    def test_initialization(self):
        self.assertIsInstance(self.server.devices, dict)
        self.assertIs(self.server.mqtt_mess5, False)
        self.assertEqual(self.server.devices['a1']['group'], '1')

    def test_timers2sprinklers_supports_enable_and_arm(self):
        timers = {'Timers': 'ON'}
        for i in range(1, 7):
            timers[f'Timer{2 * i - 1}'] = {
                'Enable': 1, 'Time': '06:00', 'Output': 1, 'Days': '1111111'}
            timers[f'Timer{2 * i}'] = {'Time': '06:10'}
        sprinklers = self.server.timers2sprinklers(timers)
        self.assertEqual(sprinklers['Timers'], 'ON')
        self.assertEqual(sprinklers['Sprinkler1']['Duration'], 10)
        self.assertEqual(sprinklers['Sprinkler1']['Arm'], 1)

    @patch('subprocess.run')
    def test_action_route_group1(self, mock_subprocess):
        response = self.client.get('/a1/on')
        self.assertEqual(response.status_code, 200)
        mock_subprocess.assert_called_with(
            ['nc', '-N', 'hcontrol.local', '1099'],
            input=b'rf a1 on\n',
            check=True,
            timeout=10,
        )

    @patch('subprocess.run')
    def test_action_route_group2(self, mock_subprocess):
        response = self.client.get('/outdoor_side/on')
        self.assertEqual(response.status_code, 200)
        mock_subprocess.assert_called_with(
            ['mosquitto_pub', '-h', 'hcontrol.local',
             '-t', 'cmnd/tasmota_outdoor_side/power1', '-m', 'on'],
            check=True,
            timeout=10,
        )

    @patch('subprocess.run')
    def test_action_route_unknown_device(self, _subprocess):
        self.assertEqual(self.client.get('/nope/on').status_code, 404)

    @patch('subprocess.run')
    def test_action_failure_is_reported_not_swallowed(self, mock_subprocess):
        mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd='nc', timeout=10)
        response = self.client.get('/a1/on')
        self.assertEqual(response.status_code, 200)
        self.assertIn('command failed', response.get_data(as_text=True))
        self.assertEqual(self.server.devices['a1']['state'], 'off')  # state unchanged

    @patch('subprocess.run')
    def test_sprinkler_group_uses_relay_number(self, mock_subprocess):
        self.client.get('/spare8/on')
        mock_subprocess.assert_called_with(
            ['mosquitto_pub', '-h', 'hcontrol.local',
             '-t', 'cmnd/tasmota_sprinklers/power8', '-m', 'on'],
            check=True,
            timeout=10,
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
