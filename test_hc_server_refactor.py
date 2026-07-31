import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json

# Add the project directory to sys.path
sys.path.append('/home/jerry/Downloads/projects/home_control')

from hc_server import HomeControlServer

class TestHomeControlServer(unittest.TestCase):
    def setUp(self):
        # Mock MQTT client before instantiating the server
        self.mqtt_patcher = patch('paho.mqtt.client.Client')
        self.mock_mqtt_client_class = self.mqtt_patcher.start()
        self.mock_mqtt_client = self.mock_mqtt_client_class.return_value
        
        # Mock config loading
        mock_config_data = json.dumps({
            "devices": {
                "a1": {"name": "Kitchen", "state": "off", "group": "1", "swno": "1", "dimr": "0"},
                "outdoor_side": {"name": "Garage side", "state": "off", "group": "2", "swno": "1", "dimr": "0"}
            }
        })
        self.config_patcher = patch('builtins.open', new_callable=unittest.mock.mock_open, read_data=mock_config_data)
        self.mock_open = self.config_patcher.start()
        
        self.server = HomeControlServer()
        self.server.app.config['TESTING'] = True
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.mqtt_patcher.stop()
        self.config_patcher.stop()

    def test_initialization(self):
        self.assertIsInstance(self.server.devices, dict)
        self.assertTrue(self.server.mqtt_mess5 is False)

    def test_timers2sprinklers(self):
        # Test data
        timers = {
            'Timers': 'ON',
            'Timer1': {'Arm': 1, 'Time': '06:00', 'Output': 1, 'Days': '1111111'},
            'Timer2': {'Time': '06:10'}, # 10 mins duration
            # Add dummy data for other timers to avoid errors if the loop expects them
        }
        # Fill in the rest
        for i in range(1, 7):
            if f'Timer{2*i+1}' not in timers:
                timers[f'Timer{2*i+1}'] = {'Arm': 0, 'Time': '00:00', 'Output': i+1, 'Days': '0000000'}
                timers[f'Timer{2*i+2}'] = {'Time': '00:00'}

        sprinklers = self.server.timers2sprinklers(timers)
        self.assertEqual(sprinklers['Timers'], 'ON')
        self.assertEqual(sprinklers['Sprinkler1']['Duration'], 10)
        self.assertEqual(sprinklers['Sprinkler1']['Arm'], 1)

    @patch('subprocess.run')
    def test_action_route_group1(self, mock_subprocess):
        # Test action for group 1 device (nc)
        response = self.client.get('/a1/on')
        self.assertEqual(response.status_code, 200)
        
        # Verify subprocess call
        # Expected: echo rf a1 on | nc -N hcontrol.local 1099
        # The code uses input argument for the echo part
        mock_subprocess.assert_called_with(
            ['nc', '-N', 'hcontrol.local', '1099'],
            input=b'rf a1 on\n',
            check=True
        )

    @patch('subprocess.run')
    def test_action_route_group2(self, mock_subprocess):
        # Test action for group 2 device (mosquitto_pub)
        # 'outdoor_side' -> group 2, swno 1
        response = self.client.get('/outdoor_side/on')
        self.assertEqual(response.status_code, 200)
        
        mock_subprocess.assert_called_with(
            ['mosquitto_pub', '-h', 'hcontrol.local', '-t', 'cmnd/tasmota_outdoor_side/power1', '-m', 'on'],
            check=True
        )

if __name__ == '__main__':
    unittest.main()
