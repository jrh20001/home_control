#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""hc_server.py"""

import os
import json
import time
import logging
import subprocess
import shlex
from datetime import datetime, timedelta
from flask import Flask, render_template, request
import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='hc_server.log',
    filemode='a'
)
logger = logging.getLogger(__name__)

class HomeControlServer:
    def __init__(self):
        self.app = Flask(__name__)
        self.mqtt_client = mqtt.Client()
        self.timers = {}
        self.sprinklers = {}
        self.mqtt_mess5 = False
        
        self.load_config()
        
        self.snames_output_index = [
            'Back East', 'Front South', 'Front North',
            'Front Drip', 'Back West', 'Back Drip'
        ]

        self.setup_routes()
        self.setup_mqtt()

    def load_config(self):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                
            mqtt_config = config.get('mqtt', {})
            self.MQTT_BROKER = mqtt_config.get('broker', "hcontrol.local")
            self.MQTT_PORT = mqtt_config.get('port', 1883)
            self.MQTT_TOPIC_CMND = mqtt_config.get('topic_cmnd', "cmnd/tasmota_sprinklers/")
            self.MQTT_TOPIC_STAT = mqtt_config.get('topic_stat', "stat/tasmota_sprinklers/RESULT")
            
            self.devices = config.get('devices', {})
            
        except Exception as e:
            logger.error(f"Failed to load config.json: {e}")
            raise

    def setup_routes(self):
        self.app.add_url_rule('/', 'home', self.home)
        self.app.add_url_rule('/<dev>/<action>', 'action', self.action)
        self.app.add_url_rule('/config', 'config', self.config, methods=['GET', 'POST'])

    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        try:
            self.mqtt_client.connect(self.MQTT_BROKER, self.MQTT_PORT, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")

    def on_connect(self, client, userdata, flags, rc):
        logger.info(f"Connected to MQTT broker with result code {rc}")
        client.subscribe(self.MQTT_TOPIC_STAT)
        self.mqtt_mess5 = False
        client.publish(self.MQTT_TOPIC_CMND + "Timers", json.dumps(''))

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if any(k in payload for k in ['Timers', 'Timers1', 'Timers2', 'Timers3', 'Timers4']):
                self.timers.update(self.parse_timers(payload))
                if 'Timers4' in payload:
                    self.sprinklers = self.timers2sprinklers(self.timers)
                    self.mqtt_mess5 = True
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON payload from mqtt broker: {msg.payload}")
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def parse_timers(self, payload):
        parsed_timers = {}
        try:
            for key, value in payload.items():
                if key == 'Timers':
                    parsed_timers[key] = value
                else:
                    for timer_name, timer_data in value.items():
                        parsed_timers[timer_name] = timer_data
        except Exception as e:
            logger.error(f"Failed to parse timers: {e}")
        return parsed_timers

    def timers2sprinklers(self, timers):
        nsprinklers = {}
        if timers.get('Timers') == 'ON':
            nsprinklers['Timers'] = 'ON'
        else:
            nsprinklers['Timers'] = 'OFF'
        
        for i in range(6):
            try:
                timer_on  = timers.get(f'Timer{2 * i + 1}')
                timer_off = timers.get(f'Timer{2 * i + 2}')
                if not timer_on or not timer_off:
                    continue
                    
                time1     = datetime.strptime(timer_on['Time'], '%H:%M')
                time2     = datetime.strptime(timer_off['Time'], '%H:%M')
                time_dif  = time2 - time1
                mins_dur  = int(time_dif.total_seconds() / 60)
                
                nsprinklers[f'Sprinkler{i + 1}'] = {
                    'sprinkler_no'   : f'Sprinkler{i + 1}',
                    'sprinkler_name' : self.snames_output_index[timer_on['Output'] - 1],
                    'Arm'            : int(timer_on['Arm']),
                    'Time'           : timer_on['Time'],
                    'Duration'       : int(mins_dur),
                    'Days'           : timer_on['Days'],
                    'Output'         : int(timer_on['Output']),
                }
            except Exception as e:
                logger.error(f"Error converting timer {i}: {e}")
        return nsprinklers

    def sprinklers2timers(self, sprinklers):
        ntimers = {}
        if sprinklers.get('Timers') == 'ON':
            ntimers['Timers'] = 'ON'
        else:
            ntimers['Timers'] = 'OFF'
            
        for i in range(6):
            try:
                spr_no = sprinklers.get(f'Sprinkler{i + 1}')
                if not spr_no:
                    continue
                    
                new_time = datetime.strptime(spr_no['Time'], "%H:%M") + timedelta(minutes=int(spr_no['Duration']))
                
                ntimers[f'Timer{2 * i + 1}'] = {
                    'Arm'    : int(spr_no['Arm']),
                    'Mode'   : 0,
                    'Time'   : spr_no['Time'],
                    'Window' : 0,
                    'Days'   : spr_no['Days'],
                    'Repeat' : 1,
                    'Output' : int(spr_no['Output']),
                    'Action' : 1
                }
                ntimers[f'Timer{2 * i + 2}'] = {
                    'Arm'    : 1,
                    'Mode'   : 0,
                    'Time'   : new_time.strftime("%H:%M"),
                    'Window' : 0,
                    'Days'   : '1111111',
                    'Repeat' : 1,
                    'Output' : int(spr_no['Output']),
                    'Action' : 0
                }
            except Exception as e:
                logger.error(f"Error converting sprinkler {i}: {e}")

        for i in range(6, 8):
            ntimers[f'Timer{2 * i + 1}'] = {
                'Arm'    : 0, 'Mode' : 0, 'Time' : "00:00", 'Window' : 0,
                'Days'   : '0000000', 'Repeat' : 1, 'Output' : 8, 'Action' : 0
            }
            ntimers[f'Timer{2 * i + 2}'] = {
                'Arm'    : 0, 'Mode' : 0, 'Time' : "00:00", 'Window' : 0,
                'Days'   : '0000000', 'Repeat' : 0, 'Output' : 8, 'Action' : 0
            }
        return ntimers

    def publish_mqtt_updates(self, timers):
        for key, value in timers.items():
            payload = value if isinstance(value, str) else json.dumps(value)
            self.mqtt_client.publish(self.MQTT_TOPIC_CMND + key, payload)

    def home(self):
        templateData = {
            'ptitle'  : 'Lights A',
            'group'   : '1',
            'message' : '',
            'devices' : self.devices
        }
        return render_template('home.html', **templateData)

    def action(self, dev, action):
        device = self.devices.get(dev)
        if not device:
            return f"Device {dev} not found", 404
            
        group = device.get('group')
        device_name = device.get('name')
        
        if action in ['on', 'off']:
            self.devices[dev]['state'] = action
            
        swno = device['swno']
        message = f"{device_name} {action}"
        
        ptitle = 'Lights A'
        cmd = []
        
        if group == '1':
            # Using nc via subprocess safely
            # echo rf {dev} {action} | nc -N hcontrol.local 1099
            # We can construct the input and pass it to nc
            input_str = f"rf {dev} {action}\n"
            cmd = ['nc', '-N', 'hcontrol.local', '1099']
            try:
                subprocess.run(cmd, input=input_str.encode(), check=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Command failed: {e}")
            ptitle = 'Lights A'

        elif group == '2':
            # mosquitto_pub -h hcontrol.local -t cmnd/tasmota_{dev}/power{swno} -m {action}
            cmd = [
                'mosquitto_pub', '-h', 'hcontrol.local',
                '-t', f'cmnd/tasmota_{dev}/power{swno}',
                '-m', action
            ]
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Command failed: {e}")
            ptitle = 'Lights B'

        elif group == '3':
            # mosquitto_pub -h hcontrol.local -t cmnd/tasmota_sprinklers/power{swno} -m {action}
            cmd = [
                'mosquitto_pub', '-h', 'hcontrol.local',
                '-t', f'cmnd/tasmota_sprinklers/power{swno}',
                '-m', action
            ]
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                logger.error(f"Command failed: {e}")
            ptitle = 'Sprinklers'

        logger.info(f"Action: {dev} {action} | Group: {group}")
        
        templateData = {
           'ptitle'  : ptitle,
           'group'   : group,
           'message' : message,
           'devices' : self.devices
        }
        return render_template('home.html', **templateData)

    def config(self):
        if request.method == "GET":
            logger.info('GET config')
            self.mqtt_mess5 = False
            self.mqtt_client.publish(self.MQTT_TOPIC_CMND + "Timers", json.dumps(''))
            
            # Wait for MQTT response with timeout
            start_time = time.time()
            while not self.mqtt_mess5:
                time.sleep(0.05)
                if time.time() - start_time > 5:
                    logger.warning("Timeout waiting for MQTT data")
                    break
            
            return render_template("config.html", sprinklers=self.sprinklers) 

        if request.method == "POST":
            try:
                logger.info('POST config')
                self.sprinklers['Timers'] = 'ON' if request.form.get("Timers") == 'on' else 'OFF'
                
                for i in range(1, 7):
                    sprinkler_no = f"Sprinkler{i}"
                    Arm      = 1 if request.form.get(f"Arm{i}") == 'on' else 0
                    Time     = request.form.get(f"Time{i}")
                    Duration = int(request.form.get(f"Duration{i}", 0))
                    Days     = "".join(['1' if request.form.get(f"Days{i}{j}") == 'on' else '0' for j in range(7)])
                    
                    if sprinkler_no not in self.sprinklers:
                        self.sprinklers[sprinkler_no] = {}
                        
                    self.sprinklers[sprinkler_no]['Arm']      = Arm
                    self.sprinklers[sprinkler_no]['Time']     = Time
                    self.sprinklers[sprinkler_no]['Duration'] = Duration
                    self.sprinklers[sprinkler_no]['Days']     = Days
            
                self.timers = self.sprinklers2timers(self.sprinklers)                
                self.publish_mqtt_updates(self.timers)

            except Exception as e:
                logger.error(f"Error processing form data: {e}")
                
            return render_template("config.html", sprinklers=self.sprinklers)

    def run(self):
        # Security: debug=False for production
        self.app.run(host='0.0.0.0', port=80, debug=False)

if __name__ == '__main__':
    server = HomeControlServer()
    server.run()
