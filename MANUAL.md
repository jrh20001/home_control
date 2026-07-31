# Home Control Application Manual

## Overview
The Home Control application is a Python-based system for managing home automation devices. It provides a web interface to control lights and sprinklers, and includes an automated scheduler based on local sunrise/sunset times.

## Features
- **Web Interface**: Control devices from any browser on the local network.
- **Device Groups**: Organized into Lights A (RF), Lights B (MQTT), and Sprinklers.
- **Automated Scheduling**: Schedule events relative to sunrise/sunset or at specific times.
- **Sprinkler Timer**: Configure sprinkler duration and days via the web UI.
- **Modern UI**: Dark mode interface with responsive design.

## Installation

### Prerequisites
- Python 3.x
- `pip` (Python package manager)
- `mosquitto` (MQTT broker) and `mosquitto-clients`
- `netcat` (`nc`)

### Dependencies
Install the required Python packages:
```bash
pip install flask paho-mqtt suntime
```

## Configuration (`config.json`)
The application is configured via `config.json`. This file contains three main sections:

### 1. MQTT
Configure your MQTT broker settings:
```json
"mqtt": {
    "broker": "hcontrol.local",
    "port": 1883,
    "topic_cmnd": "cmnd/tasmota_sprinklers/",
    "topic_stat": "stat/tasmota_sprinklers/RESULT"
}
```

### 2. Devices
Define your devices. Each device has a unique ID (e.g., "a1") and properties:
- `name`: Display name.
- `group`: "1" (RF/Lights A), "2" (MQTT/Lights B), or "3" (Sprinklers).
- `swno`: Switch number (for multi-channel devices).

### 3. Location & Schedule
Set your location for sun calculations and define automated events:
```json
"location": {
    "latitude": 37.37006,
    "longitude": -122.08839
},
"schedule": [
    {"time": "sunset", "offset": "-00:30:00", "device": "a1", "action": "on"},
    {"time": "today",  "offset": "22:00:00",  "device": "a1", "action": "off"}
]
```

## Usage

### Starting the Server
Run the server using Python:
```bash
python3 hc_server.py
```
The web interface will be available at `http://localhost:80` (or your server's IP).

### Web Interface
- **Navigation**: Use the top buttons to switch between device groups.
- **Control**: Click a device button to toggle it ON (Green) or OFF (Red).
- **Configuration**: In the "Sprinklers" group, click "Configuration" to set sprinkler timers.

### Automated Scheduling
To update the system scheduler (using `at` jobs), run:
```bash
python3 update_crontab.py
```
This script reads the `schedule` from `config.json`, calculates the exact times for today/tomorrow, and schedules the commands. It is recommended to run this script automatically (e.g., via cron) every day after midnight.

## Troubleshooting
- **Logs**: Check the console output for logs. Errors are logged with timestamps.
- **MQTT**: Ensure your MQTT broker is running and accessible.
- **Permissions**: If running on port 80, you may need `sudo` or capability configuration.
