import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json
import datetime

# Add the project directory to sys.path
sys.path.append('/home/jerry/Downloads/projects/home_control')

# Mock suntime before importing update_crontab
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
                {"time": "today", "offset": "10:00:00", "device": "a1", "action": "on"},
                {"time": "sunset", "offset": "-00:30:00", "device": "a1", "action": "off"}
            ]
        }
        
        # Mock config loading
        self.config_patcher = patch('update_crontab.load_config', return_value=self.mock_config)
        self.mock_load_config = self.config_patcher.start()
        
        # Mock subprocess
        self.subprocess_patcher = patch('subprocess.run')
        self.mock_subprocess = self.subprocess_patcher.start()
        
        # Mock suntime return values
        self.mock_sun_instance = update_crontab.Sun.return_value
        self.mock_sun_instance.get_local_sunrise_time.return_value = datetime.datetime.now()
        self.mock_sun_instance.get_local_sunset_time.return_value = datetime.datetime.now()

    def tearDown(self):
        self.config_patcher.stop()
        self.subprocess_patcher.stop()

    def test_get_time_offset(self):
        self.assertEqual(update_crontab.get_time_offset("10:00:00"), (10, 0, 0))
        self.assertEqual(update_crontab.get_time_offset("-00:30:00"), (-0, -30, 0))

    def test_main_execution(self):
        update_crontab.main()
        
        # Verify atq was called
        self.mock_subprocess.assert_any_call(['atq'], capture_output=True, text=True)
        
        # Verify at command was called for the scheduled items
        # We expect 2 calls to 'at' (one for each schedule item)
        # Note: we can't easily check the exact args because dates change, but we can check call count
        # Filter calls to 'at'
        at_calls = [call for call in self.mock_subprocess.call_args_list if call[0][0][0] == 'at']
        self.assertEqual(len(at_calls), 2)

if __name__ == '__main__':
    unittest.main()
