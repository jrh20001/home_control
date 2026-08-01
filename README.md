# Home Control

Home automation server with X10/Mochad relay control, MQTT integration, and cron-based scheduling.

## What it does

Controls lights, sprinklers, and other home automation devices through a web interface. Supports three device groups:

- **Lights A** — X10 RF modules via Mochad (CM15A/CM19A controller)
- **Lights B** — MQTT-connected devices (Tasmota-compatible smart switches)
- **Sprinklers** — MQTT-controlled irrigation zones with duration timers

Schedules events relative to local sunrise/sunset times with a cron-based scheduler.

## How it works

A Flask web server runs on your home network, providing a dark-mode web UI for controlling devices. Communication flows:

```
Web Browser ──HTTP──▶ Flask Server ──MQTT──▶ Tasmota Devices
                              ├──X10/Mochad──▶ RF Modules
                              └──cron──▶ Scheduled Events
```

### Key components

| File | Purpose |
|------|---------|
| `hc_server.py` | Flask web server with MQTT client, X10 relay, and scheduling |
| `config.json` | Device definitions, MQTT broker, location/schedule |
| `x10_mochad_relay_asyncio.py` | Async X10 relay via Mochad TCP socket |
| `update_crontab.py` | Generates crontab from sunrise/sunset-based schedule |
| `start_*.sh` | Startup scripts for each component |
| `static/style.css` | Web UI styling |
| `templates/*.html` | Web UI templates |
| `hc_server.service` / `.timer` | systemd service for auto-start |

## Installation

### Prerequisites

```bash
sudo apt-get install python3 python3-pip mosquitto mosquitto-clients netcat-openbsd
pip install flask paho-mqtt suntime
```

### Mochad (X10 controller)

Install [mochad](https://github.com/jrh20001/mochad-0.1.17) — the RF-to-Ethernet bridge for CM15A/CM19A:

```bash
# Build from source
wget https://github.com/jrh20001/mochad-0.1.17/archive/refs/tags/0.1.17.tar.gz
tar xzf 0.1.17.tar.gz && cd mochad-0.1.17
./configure && make && sudo make install
# Start mochad
sudo mochad
```

### Configuration

Edit `config.json`:

```json
{
  "mqtt": {
    "broker": "hcontrol.local",
    "port": 1883,
    "topic_cmnd": "cmnd/tasmota_sprinklers/",
    "topic_stat": "stat/tasmota_sprinklers/RESULT"
  },
  "devices": {
    "a1": { "name": "Kitchen", "group": "1", "swno": "1", "dimr": "0" },
    "b1": { "name": "Porch", "group": "2", "swno": "1", "dimr": "1" },
    "sprinkler1": { "name": "Front Lawn", "group": "3", "swno": "1", "minutes": "10" }
  },
  "location": {
    "latitude": 34.05,
    "longitude": -118.25
  },
  "schedule": [
    { "time": "sunset-30", "device": "a1", "action": "on" },
    { "time": "sunrise", "device": "a1", "action": "off" }
  ]
}
```

Device groups:
- `"1"` — Lights A (X10 RF via Mochad)
- `"2"` — Lights B (MQTT/Tasmota)
- `"3"` — Sprinklers (MQTT with duration timer)

### Running

```bash
# Start the web server
python3 hc_server.py

# Generate and install crontab for scheduled events
python3 update_crontab.py
```

Then open `http://hcontrol.local:5000/` in a browser.

### Auto-start with systemd

```bash
sudo cp hc_server.service /etc/systemd/system/
sudo cp hc_server.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hc_server.timer
```

## Usage

1. Open the web UI at `http://hcontrol.local:5000/`
2. Click devices to toggle on/off
3. Use the sprinkler panel to set zone durations and start times
4. Scheduled events run automatically via crontab

## API

The web UI communicates with the server via JSON endpoints:

- `POST /device/<id>/on` — Turn device on
- `POST /device/<id>/off` — Turn device off
- `GET /status` — Get all device states
- `POST /sprinkler/<id>/start` — Start sprinkler zone
- `POST /sprinkler/<id>/stop` — Stop sprinkler zone