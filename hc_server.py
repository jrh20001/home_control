#!/usr/bin/python3
"""hc_server.py — Home Control web server (Lights A/B, sprinklers)."""

import contextlib
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import paho.mqtt.client as mqtt
from flask import Flask, render_template, request

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hc_server.log')
_LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
try:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT,
                        filename=LOG_PATH, filemode='a')
except OSError:                      # read-only/unwritable dir: keep serving
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger(__name__)

try:
    from suntime import Sun
except ImportError:                      # keep the server importable without it
    Sun = None                           # pyright: ignore[reportAssignmentType]

MAX_SPRINKLER_MINUTES = 60
OFFSET_PARTS = 3
DAYS_PER_WEEK = 7
MAX_OFFSET_HOURS = 23

class HomeControlServer:
    def __init__(self):
        self.app = Flask(__name__)
        self.mqtt_client = mqtt.Client()
        self.timers: dict[str, Any] = {}
        self.sprinklers: dict[str, Any] = {}
        self.mqtt_mess5 = False
        self.sprinkler_wait_seconds = 5.0
        self.command_timeout = 10

        # MQTT callbacks run on paho's thread while Flask serves requests from
        # several threads, so every shared dict is guarded by one of these.
        self.sprinkler_lock = threading.RLock()
        self.light_lock = threading.RLock()

        # Lights B (group 2): every light is its own Tasmota device, so timer
        # state is tracked per device instead of in one shared dict.
        self.light_timers: dict[str, Any] = {}
        self.light_ready: dict[str, bool] = {}
        self.light_lwt: dict[str, str] = {}
        self.light_ip: dict[str, str] = self.load_ip_cache()
        self.light_wait_seconds = 3.0
        self.light_ip_wait_seconds = 1.0

        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'config.json')
        self.devices: dict[str, Any] = {}
        self.schedule: list[dict[str, Any]] = []
        self.location: dict[str, Any] = {}
        self.raw_config: dict[str, Any] = {}
        self.load_config()

        self.snames_output_index = [
            'Back East', 'Front South', 'Front North',
            'Front Drip', 'Back West', 'Back Drip'
        ]

        self.setup_routes()
        self.setup_mqtt()

    def load_config(self):
        config_path = self.config_path
        try:
            with open(config_path) as f:
                config = json.load(f)

            mqtt_config = config.get('mqtt', {})
            self.MQTT_BROKER = mqtt_config.get('broker', "hcontrol.local")
            self.MQTT_PORT = mqtt_config.get('port', 1883)
            self.MQTT_TOPIC_CMND = mqtt_config.get('topic_cmnd', "cmnd/tasmota_sprinklers/")
            self.MQTT_TOPIC_STAT = mqtt_config.get('topic_stat', "stat/tasmota_sprinklers/RESULT")

            self.devices = config.get('devices', {})
            for info in self.devices.values():
                info['group'] = str(info.get('group', ''))
                info['swno'] = str(info.get('swno', 1))
            self.location = config.get('location', {})
            self.schedule = config.get('schedule', [])
            # pristine copy for rewriting the file: never pick up runtime state
            self.raw_config = copy.deepcopy(config)

        except Exception as e:
            logger.error(f"Failed to load config.json: {e}")
            raise

    def setup_routes(self):
        self.app.add_url_rule('/', 'home', self.home)
        self.app.add_url_rule('/<dev>/<action>', 'action', self.action)
        self.app.add_url_rule('/config', 'config', self.config, methods=['GET', 'POST'])
        self.app.add_url_rule('/config_lights', 'config_lights', self.config_lights,
                              methods=['GET', 'POST'])
        self.app.add_url_rule('/config_lights_a', 'config_lights_a', self.config_lights_a,
                              methods=['GET', 'POST'])

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
        # Single-level wildcards must occupy a whole topic level ('tasmota_+' is
        # rejected by paho), so subscribe wide and route by device below.
        client.subscribe('stat/+/RESULT')
        client.subscribe('stat/+/STATUS5')
        client.subscribe('tele/+/LWT')
        self.mqtt_mess5 = False
        client.publish(self.MQTT_TOPIC_CMND + "Timers", json.dumps(''))

    def on_message(self, client, userdata, msg):
        topic = msg.topic

        if topic.startswith('tele/tasmota_') and topic.endswith('/LWT'):
            dev = topic[len('tele/tasmota_'):-len('/LWT')]
            if dev in self.light_devices():
                self.light_lwt[dev] = msg.payload.decode(errors='replace').strip()
            return

        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON payload from mqtt broker: {msg.payload}")
            return

        if not isinstance(payload, dict):
            return

        if topic == self.MQTT_TOPIC_STAT:
            self._handle_sprinkler_timers(payload)
        elif topic.startswith('stat/tasmota_') and topic.endswith('/STATUS5'):
            self._handle_status5(topic, payload)
        elif topic.startswith('stat/tasmota_') and topic.endswith('/RESULT'):
            dev = topic[len('stat/tasmota_'):-len('/RESULT')]
            if dev in self.light_devices():
                self._handle_light_timers(dev, payload)

    def _handle_sprinkler_timers(self, payload):
        with self.sprinkler_lock:
            self._update_sprinkler_timers(payload)

    def _update_sprinkler_timers(self, payload):
        try:
            if not any(k.startswith('Timer') for k in payload):
                return

            self.timers.update(self.parse_timers(payload))

            # Tasmota answers `Timers` with either one flat RESULT holding every
            # timer (Timer1..Timer16) or, on older firmware, four chunks
            # (Timers1..Timers4). Both must mark the request as satisfied.
            got_timer_defs = any(
                k.startswith('Timer') and isinstance(v, dict) and 'Time' in v
                for k, v in self.timers.items()
            )
            if 'Timers4' in payload or ('Timers' in self.timers and got_timer_defs):
                self.sprinklers = self.timers2sprinklers(self.timers)
                if not self.mqtt_mess5:
                    logger.info(f"Timers loaded: {len(self.sprinklers) - 1} sprinklers")
                self.mqtt_mess5 = True
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def _handle_light_timers(self, dev, payload):
        with self.light_lock:
            self._update_light_timers(dev, payload)

    def _update_light_timers(self, dev, payload):
        try:
            flat = self.parse_timers(payload)
            defs = {k: v for k, v in flat.items()
                    if k != 'Timers' and isinstance(v, dict) and 'Time' in v}
            if not defs:
                return
            if 'Timers' in payload:
                store = {'Timers': payload.get('Timers', flat.get('Timers', 'ON'))}
                store.update(defs)
                self.light_timers[dev] = store
                self.light_ready[dev] = True
            else:
                # single-timer echo after a save
                self.light_timers.setdefault(dev, {}).update(defs)
        except Exception as e:
            logger.error(f"Error processing light timers for {dev}: {e}")

    def _handle_status5(self, topic, payload):
        dev = topic[len('stat/tasmota_'):-len('/STATUS5')]
        if dev not in self.light_devices():
            return
        ip = (payload.get('StatusNET') or {}).get('IPAddress')
        with self.light_lock:
            if ip and self.light_ip.get(dev) != ip:
                self.light_ip[dev] = ip
                self.save_ip_cache()

    def parse_timers(self, payload):
        parsed_timers = {}
        try:
            for key, value in payload.items():
                if not isinstance(value, dict):
                    # 'Timers': 'ON' / 'OFF'
                    parsed_timers[key] = value
                elif key.startswith('Timer') and 'Time' in value:
                    # flat layout: {"Timer1": {...}, ...}
                    parsed_timers[key] = value
                else:
                    # chunked layout: {"Timers1": {"Timer1": {...}, ...}}
                    parsed_timers.update(value)
        except Exception as e:
            logger.error(f"Failed to parse timers: {e}")
        return parsed_timers

    # ---- Lights B (group 2) -------------------------------------------

    LIGHT_GROUP = '2'

    def devices_in_group(self, group):
        return {dev: d for dev, d in self.devices.items()
                if str(d.get('group')) == str(group)}

    def light_devices(self):
        return self.devices_in_group(self.LIGHT_GROUP)

    def ip_cache_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'light_ip_cache.json')

    def load_ip_cache(self):
        try:
            with open(self.ip_cache_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_ip_cache(self):
        path = self.ip_cache_path()
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.light_ip_cache.')
            with os.fdopen(fd, 'w') as f:
                json.dump(self.light_ip, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"Could not write light IP cache: {e}")
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    @staticmethod
    def _as_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _timer_part(self, timer):
        """Split a Tasmota timer into the pieces the page edits."""
        timer = timer or {}
        raw = str(timer.get('Time', '00:00')).strip()
        sign = '-' if raw.startswith('-') else '+'
        mode = self._as_int(timer.get('Mode', 0))
        return {
            'Enable': self._as_int(timer.get('Enable', timer.get('Arm', 0))),
            'Mode': mode if mode in (0, 1, 2) else 0,
            'Sign': sign,
            'Time': raw.lstrip('+-').strip() or '00:00',
            'Window': self._as_int(timer.get('Window', 0)),
            'Repeat': self._as_int(timer.get('Repeat', 1), 1),
            'Days': str(timer.get('Days') or '0000000'),
        }

    def light_row(self, dev, device):
        timers = self.light_timers.get(dev, {})
        on = self._timer_part(timers.get('Timer1'))
        off = self._timer_part(timers.get('Timer2'))

        duration = None
        if 'Timer1' in timers and 'Timer2' in timers and on['Mode'] == 0 and off['Mode'] == 0:
            try:
                t_on = datetime.strptime(on['Time'], '%H:%M')
                t_off = datetime.strptime(off['Time'], '%H:%M')
                mins = int((t_off - t_on).total_seconds() // 60)
                duration = mins if mins > 0 else mins + 24 * 60
            except ValueError:
                duration = None

        days = on['Days'] if len(on['Days']) == DAYS_PER_WEEK else off['Days']
        return {
            'dev': dev,
            'name': device.get('name', dev),
            'host': f'tasmota_{dev}',
            'ip': self.light_ip.get(dev),
            'online': self.light_lwt.get(dev) != 'Offline',
            'reachable': bool(self.light_ready.get(dev)),
            'Timers': 'ON' if timers.get('Timers') == 'ON' else 'OFF',
            'on': on,
            'off': off,
            'days': days if len(days) == DAYS_PER_WEEK else '0000000',
            'duration': duration,
        }

    def light_rows(self):
        return [self.light_row(dev, cfg)
                for dev, cfg in self.light_devices().items()]

    def timer_from_form(self, dev, which):
        form = request.form
        try:
            mode = int(form.get(f'{which}mode_{dev}', 0))
            window = int(form.get(f'{which}window_{dev}', 0) or 0)
            repeat = int(form.get(f'{which}repeat_{dev}', 1) or 0)
        except ValueError:
            return None

        if mode not in (0, 1, 2):
            return None
        enable = 1 if form.get(f'{which}en_{dev}') == 'on' else 0
        tstr = (form.get(f'{which}time_{dev}') or '').strip()
        if not re.match(r'^([01]?\d|2[0-3]):[0-5]\d$', tstr):
            return None
        hh, mm = tstr.split(':')
        tstr = f'{int(hh):02d}:{mm}'
        if mode != 0 and form.get(f'{which}sign_{dev}') == '-':
            tstr = '-' + tstr
        return {'Enable': enable, 'Mode': mode, 'Time': tstr,
                'Window': window, 'Repeat': repeat}

    def _fetch_light_timers(self):
        """Ask every Lights B device for its timers; returns a warning or ''."""
        lights = self.light_devices()
        if not self.mqtt_client.is_connected():
            logger.warning('GET config_lights: MQTT not connected')
            return 'MQTT broker is not connected; showing the last known values.'

        with self.light_lock:
            self.light_ready = dict.fromkeys(lights, False)
            for dev in lights:
                self.mqtt_client.publish(f'cmnd/tasmota_{dev}/Timers', '')
                self.mqtt_client.publish(f'cmnd/tasmota_{dev}/Status', '5')

        # The lock must be released here: the MQTT callbacks that set
        # light_ready/light_ip need it, so holding it would guarantee a timeout.
        waiting = [dev for dev in lights if self.light_lwt.get(dev) != 'Offline']
        deadline = time.time() + self.light_wait_seconds
        while (time.time() < deadline
               and not all(self.light_ready.get(d) for d in waiting)):
            time.sleep(0.05)
        if any(self.light_ip.get(d) is None for d in waiting):
            ip_deadline = time.time() + self.light_ip_wait_seconds
            while (time.time() < ip_deadline
                   and any(self.light_ip.get(d) is None for d in waiting)):
                time.sleep(0.05)

        missing = [d for d in waiting if not self.light_ready.get(d)]
        if missing:
            return ('No answer from: ' + ', '.join(sorted(missing))
                    + '; those rows show last known values.')
        return ''

    def _collect_light_payloads(self, form):
        """Validate the Lights B form; returns (payloads, errors)."""
        payloads, errors = [], []
        for dev, device in self.light_devices().items():
            if f'ontime_{dev}' not in form:
                continue                      # unreachable row: inputs disabled
            output = self._as_int(device.get('swno'), -1)
            if output < 0:
                errors.append(f'{dev}: bad swno in config.json')
                continue

            on = self.timer_from_form(dev, 'on')
            off = self.timer_from_form(dev, 'off')
            if not on or not off:
                errors.append(f'{dev}: invalid time or mode')
                continue

            master = 'ON' if form.get(f'timers_{dev}') == 'on' else 'OFF'
            days = ''.join('1' if form.get(f'days_{dev}{j}') == 'on' else '0'
                           for j in range(DAYS_PER_WEEK))
            timers = {}
            for which, part, act in (('Timer1', on, 1), ('Timer2', off, 0)):
                timers[which] = {
                    'Enable': part['Enable'],
                    'Mode': part['Mode'],
                    'Time': part['Time'],
                    'Window': part['Window'],
                    'Days': days,
                    'Repeat': part['Repeat'],
                    'Output': output,
                    'Action': act,
                }
            timers['Timers'] = master
            payloads.append((dev, timers))
        return payloads, errors

    def config_lights(self):
        """Lights B timer page. Every light is a separate Tasmota device."""
        if request.method == "GET":
            logger.info('GET config_lights')
            warning = self._fetch_light_timers()
            with self.light_lock:
                rows = self.light_rows()
            return render_template('config_lights.html', lights=rows,
                                   message=warning,
                                   message_kind='error' if warning else 'ok')

        with self.light_lock:
            logger.info('POST config_lights')
            if not self.mqtt_client.is_connected():
                return render_template('config_lights.html', lights=self.light_rows(),
                                       message='Not saved: MQTT broker is not connected.',
                                       message_kind='error'), 503

            payloads, errors = self._collect_light_payloads(request.form)
            if errors:
                logger.warning('config_lights POST rejected: ' + '; '.join(errors))
                return render_template('config_lights.html', lights=self.light_rows(),
                                       message='Not saved: ' + '; '.join(errors),
                                       message_kind='error'), 400
            if not payloads:
                return render_template('config_lights.html', lights=self.light_rows(),
                                       message='Nothing to save: no reachable lights.',
                                       message_kind='error'), 400

            for dev, timers in payloads:
                for which in ('Timer1', 'Timer2'):
                    self.mqtt_client.publish(f'cmnd/tasmota_{dev}/{which}',
                                             json.dumps(timers[which]))
                self.mqtt_client.publish(f'cmnd/tasmota_{dev}/Timers', timers['Timers'])
                self.light_timers[dev] = timers
                self.light_ready[dev] = True
                logger.info(f'config_lights: saved {dev} (timers {timers["Timers"]})')

            return render_template('config_lights.html', lights=self.light_rows(),
                                   message=f'Saved {len(payloads)} light(s).',
                                   message_kind='ok')

    # ---- Lights A (group 1): schedule stored in config.json ------------

    A_GROUP = '1'
    BASE_TIMES = ('today', 'tomorrow', 'sunrise', 'sunset')
    OFFSET_RE = re.compile(r'^(\d{1,2}):([0-5]\d):([0-5]\d)$')

    def sun_times(self):
        try:
            if Sun is None:
                raise RuntimeError('suntime is not installed')
            lat = float(self.location.get('latitude', 37.37006))
            lon = float(self.location.get('longitude', -122.08839))
            sun = Sun(lat, lon)
            today = date.today()
            return (sun.get_local_sunrise_time(today),
                    sun.get_local_sunset_time(today))
        except Exception as e:
            logger.warning(f"Could not compute sun times: {e}")
            return None, None

    def parse_offset(self, sign, value):
        """Normalise an offset to HH:MM:SS or -HH:MM:SS."""
        text = (value or '').strip().lstrip('+-')
        m = self.OFFSET_RE.match(text)
        if not m:
            return None
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if hh > MAX_OFFSET_HOURS:
            return None
        normalised = f'{hh:02d}:{mm:02d}:{ss:02d}'
        return ('-' + normalised) if str(sign) == '-' else normalised

    def resolve_run(self, base, offset, sunrise, sunset):
        """Mirror update_crontab.py: hours/minutes move the time, seconds are a
        sleep/stagger delay in the generated at job."""
        if sunrise is None or sunset is None or base not in self.BASE_TIMES:
            return None, 0
        midnight = datetime.combine(date.today(), dtime.min)
        if sunrise.tzinfo:
            midnight = midnight.replace(tzinfo=sunrise.tzinfo)
        bases = {
            'today': midnight,
            'tomorrow': midnight + timedelta(days=1),
            'sunrise': sunrise + timedelta(days=1),
            'sunset': sunset + timedelta(days=1),
        }
        text = (offset or '').strip()
        sign = -1 if text.startswith('-') else 1
        parts = text.lstrip('+-').split(':')
        if len(parts) != OFFSET_PARTS:
            return None, 0
        try:
            hh, mm, ss = (int(x) for x in parts)
        except ValueError:
            return None, 0
        target = bases[base] + sign * timedelta(hours=hh, minutes=mm)
        return target.strftime('%a %H:%M'), ss

    def schedule_rows(self):
        sunrise, sunset = self.sun_times()
        groups = self.devices_in_group(self.A_GROUP)
        per_dev = {dev: [] for dev in groups}
        for idx, entry in enumerate(self.schedule):
            dev = entry.get('device')
            if dev not in per_dev:
                continue
            offset = str(entry.get('offset', '00:00:00')).strip()
            sign = '-' if offset.startswith('-') else '+'
            base = str(entry.get('time', ''))
            action = str(entry.get('action', ''))
            next_run, stagger = self.resolve_run(base, offset, sunrise, sunset)
            per_dev[dev].append({
                'idx': idx,
                'action': action,
                'base': base,
                'sign': sign,
                'offset': offset.lstrip('+-'),
                'next_run': next_run,
                'stagger': stagger,
                'invalid': base not in self.BASE_TIMES or action not in ('on', 'off'),
            })
        return [{'dev': dev, 'name': cfg.get('name', dev), 'entries': per_dev[dev]}
                for dev, cfg in groups.items()]

    def save_config_json(self):
        """Atomic rewrite of config.json, preserving owner/mode, with a backup."""
        path = self.config_path
        st = os.stat(path)
        backup = f'{path}.bak.{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}'
        shutil.copy2(path, backup)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.config.json.')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.raw_config, f, indent=4)
                f.write('\n')
            os.chmod(tmp, st.st_mode & 0o7777)
            with contextlib.suppress(OSError):
                os.chown(tmp, st.st_uid, st.st_gid)   # keep the file's owner
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return backup

    def reload_config(self):
        """Re-read config.json, keeping the runtime on/off state of the buttons."""
        states = {dev: info.get('state') for dev, info in self.devices.items()}
        self.load_config()
        for dev, state in states.items():
            if dev in self.devices and state is not None:
                self.devices[dev]['state'] = state

    def run_update_crontab(self):
        """Regenerate at jobs. Must run as ubuntu: update_crontab.py clears the
        queue it can see, so root-owned jobs would leak and duplicate forever."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(script_dir, 'update_crontab.py')

        def _run(cmd):
            if os.geteuid() == 0:
                try:
                    return subprocess.run(cmd, cwd=script_dir, capture_output=True,
                                          text=True, timeout=180, check=False,
                                          user='ubuntu')
                except (OSError, ValueError, KeyError, TypeError) as e:
                    logger.warning(f'Could not drop to ubuntu for {cmd[0]}: {e}')
            return subprocess.run(cmd, cwd=script_dir, capture_output=True,
                                  text=True, timeout=180, check=False)

        proc = _run([sys.executable, script])
        queued = 0
        try:
            q = _run(['atq'])
            queued = len([line for line in q.stdout.splitlines() if line.strip()])
        except Exception as e:
            logger.warning(f"Could not read at queue: {e}")
        ok = proc.returncode == 0
        if ok:
            logger.info(f"Schedule regenerated: {queued} at jobs")
        else:
            logger.error(f"update_crontab failed: {(proc.stderr or '')[-500:]}")
        return ok, queued, (proc.stderr or '').strip()[-300:]

    def _schedule_context(self, **extra):
        sunrise, sunset = self.sun_times()
        context = {
            'lights': self.schedule_rows(),
            'sunrise': sunrise.strftime('%H:%M') if sunrise else None,
            'sunset': sunset.strftime('%H:%M') if sunset else None,
            'base_times': self.BASE_TIMES,
        }
        context.update(extra)
        return context

    def _collect_schedule_entries(self, form, groups):
        """Rebuild config.json['schedule'] from the posted form."""
        entries, errors = [], []
        for idx, entry in enumerate(self.schedule):
            dev = entry.get('device')
            if dev not in groups or f'sched_{idx}_action' not in form:
                entries.append(entry)      # not editable here, or newer than page
                continue
            if form.get(f'sched_{idx}_delete'):
                continue

            action = form.get(f'sched_{idx}_action')
            base = form.get(f'sched_{idx}_base')
            offset = self.parse_offset(form.get(f'sched_{idx}_sign'),
                                       form.get(f'sched_{idx}_offset'))
            if action not in ('on', 'off') or base not in self.BASE_TIMES or offset is None:
                errors.append(f'{dev} entry {idx + 1}: invalid action/time/offset')
                continue
            old_raw = str(entry.get('offset', '')).strip()
            old_sign = '-' if old_raw.startswith('-') else '+'
            if self.parse_offset(old_sign, old_raw) == offset:
                offset = old_raw           # unchanged: keep the file's spelling
            updated = dict(entry)
            updated.update({'time': base, 'offset': offset,
                            'device': dev, 'action': action})
            entries.append(updated)

        for dev in groups:
            raw = (form.get(f'new_{dev}_offset') or '').strip()
            if not raw:
                continue                   # blank spare row
            action = form.get(f'new_{dev}_action')
            base = form.get(f'new_{dev}_base')
            offset = self.parse_offset(form.get(f'new_{dev}_sign'), raw)
            if action not in ('on', 'off') or base not in self.BASE_TIMES or offset is None:
                errors.append(f'{dev} new entry: invalid action/time/offset')
                continue
            entries.append({'time': base, 'offset': offset,
                            'device': dev, 'action': action})
        return entries, errors

    def config_lights_a(self):
        """Lights A schedule page: the only configuration kept in config.json."""
        if request.method == "GET":
            logger.info('GET config_lights_a')
            return render_template('config_lights_a.html', **self._schedule_context())

        logger.info('POST config_lights_a')
        groups = self.devices_in_group(self.A_GROUP)
        entries, errors = self._collect_schedule_entries(request.form, groups)

        if errors:
            logger.warning('config_lights_a POST rejected: ' + '; '.join(errors))
            return render_template(
                'config_lights_a.html',
                **self._schedule_context(message='Not saved: ' + '; '.join(errors),
                                         message_kind='error')), 400

        try:
            self.raw_config['schedule'] = entries
            backup = self.save_config_json()
            self.reload_config()
            ok, queued, detail = self.run_update_crontab()
        except Exception as e:
            logger.error(f'config_lights_a save failed: {e}')
            with contextlib.suppress(Exception):
                self.reload_config()       # memory must match what is on disk
            return render_template(
                'config_lights_a.html',
                **self._schedule_context(message=f'Not saved: {e}',
                                         message_kind='error')), 500

        message = (f'Saved {len(entries)} schedule entries to config.json '
                   f'(backup {os.path.basename(backup)}); {queued} at jobs queued.')
        if not ok:
            message += f' Schedule regeneration failed: {detail}'
        return render_template('config_lights_a.html',
                               **self._schedule_context(
                                   message=message,
                                   message_kind='ok' if ok else 'error'))

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

                # Current Tasmota firmware reports the armed flag as 'Enable';
                # older firmware used 'Arm'.
                arm = timer_on.get('Enable', timer_on.get('Arm', 0))

                nsprinklers[f'Sprinkler{i + 1}'] = {
                    'sprinkler_no'   : f'Sprinkler{i + 1}',
                    'sprinkler_name' : self.snames_output_index[int(timer_on['Output']) - 1],
                    'Arm'            : int(arm),
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
                    'Enable' : int(spr_no['Arm']),
                    'Mode'   : 0,
                    'Time'   : spr_no['Time'],
                    'Window' : 0,
                    'Days'   : spr_no['Days'],
                    'Repeat' : 1,
                    'Output' : int(spr_no['Output']),
                    'Action' : 1
                }
                ntimers[f'Timer{2 * i + 2}'] = {
                    'Enable' : 1,
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
                'Enable' : 0, 'Mode' : 0, 'Time' : "00:00", 'Window' : 0,
                'Days'   : '0000000', 'Repeat' : 1, 'Output' : 8, 'Action' : 0
            }
            ntimers[f'Timer{2 * i + 2}'] = {
                'Enable' : 0, 'Mode' : 0, 'Time' : "00:00", 'Window' : 0,
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
        swno = device['swno']
        message = f"{device_name} {action}"
        ptitle = 'Lights A'
        cmd = []
        command_ok = True

        if group == '1':
            # echo rf {dev} {action} | nc -N hcontrol.local 1099
            input_str = f"rf {dev} {action}\n"
            cmd = ['nc', '-N', 'hcontrol.local', '1099']
            try:
                subprocess.run(cmd, input=input_str.encode(), check=True,
                               timeout=self.command_timeout)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.error(f"Command failed: {e}")
                command_ok = False
            ptitle = 'Lights A'

        elif group == '2':
            cmd = [
                'mosquitto_pub', '-h', 'hcontrol.local',
                '-t', f'cmnd/tasmota_{dev}/power{swno}',
                '-m', action
            ]
            try:
                subprocess.run(cmd, check=True, timeout=self.command_timeout)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.error(f"Command failed: {e}")
                command_ok = False
            ptitle = 'Lights B'

        elif group == '3':
            cmd = [
                'mosquitto_pub', '-h', 'hcontrol.local',
                '-t', f'cmnd/tasmota_sprinklers/power{swno}',
                '-m', action
            ]
            try:
                subprocess.run(cmd, check=True, timeout=self.command_timeout)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.error(f"Command failed: {e}")
                command_ok = False
            ptitle = 'Sprinklers'

        if action in ('on', 'off'):
            if command_ok:
                self.devices[dev]['state'] = action
            else:
                message = f"{device_name} {action} (command failed)"

        logger.info(f"Action: {dev} {action} | Group: {group} | ok={command_ok}")

        templateData = {
           'ptitle'  : ptitle,
           'group'   : group,
           'message' : message,
           'devices' : self.devices
        }
        return render_template('home.html', **templateData)

    def config(self):
        """Sprinkler timer page. All sprinkler timers live on one Tasmota device."""
        if request.method == "GET":
            logger.info('GET config')
            warning = ''
            if not self.mqtt_client.is_connected():
                warning = ('MQTT broker is not connected; showing the last known '
                           'values.')
                logger.warning('GET config: MQTT not connected')
            else:
                with self.sprinkler_lock:
                    self.mqtt_mess5 = False
                    self.mqtt_client.publish(self.MQTT_TOPIC_CMND + "Timers",
                                             json.dumps(''))
                # release before waiting: the MQTT callback needs this lock
                deadline = time.time() + self.sprinkler_wait_seconds
                while not self.mqtt_mess5 and time.time() < deadline:
                    time.sleep(0.05)
                if not self.mqtt_mess5:
                    warning = ('The sprinkler controller did not answer; showing '
                               'the last known values.')
                    logger.warning('Timeout waiting for MQTT data')
            with self.sprinkler_lock:
                sprinklers = self.sprinklers
            return render_template("config.html", sprinklers=sprinklers,
                                   message=warning,
                                   message_kind='error' if warning else 'ok')

        with self.sprinkler_lock:
            logger.info('POST config')
            if not self.mqtt_client.is_connected():
                return render_template("config.html", sprinklers=self.sprinklers,
                                       message='Not saved: MQTT broker is not connected.',
                                       message_kind='error'), 503

            updated, errors = self._collect_sprinkler_state(request.form)
            if errors:
                logger.warning('config POST rejected: ' + '; '.join(errors))
                return render_template("config.html", sprinklers=self.sprinklers,
                                       message='Not saved: ' + '; '.join(errors),
                                       message_kind='error'), 400

            self.sprinklers = updated
            self.timers = self.sprinklers2timers(self.sprinklers)
            self.publish_mqtt_updates(self.timers)
            return render_template("config.html", sprinklers=self.sprinklers,
                                   message='Sprinkler timers saved.', message_kind='ok')

    def _collect_sprinkler_state(self, form):
        """Validate the sprinkler form; returns (state, errors)."""
        errors = []
        updated: dict[str, Any] = {
            'Timers': 'ON' if form.get("Timers") == 'on' else 'OFF',
        }
        recovered = self.timers2sprinklers(self.timers)

        for i in range(1, 7):
            key = f"Sprinkler{i}"
            existing = self.sprinklers.get(key) or recovered.get(key) or {}
            output = self._as_int(existing.get('Output'), -1)
            if output < 0:
                errors.append(f'{key}: controller state unknown, reload the page')
                continue

            time_str = (form.get(f"Time{i}") or '').strip()
            if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', time_str):
                errors.append(f'{key}: invalid start time "{time_str}"')
                continue

            duration = self._as_int(form.get(f"Duration{i}"), 0)
            if not 1 <= duration <= MAX_SPRINKLER_MINUTES:
                errors.append(f'{key}: duration must be 1-{MAX_SPRINKLER_MINUTES} minutes')
                continue

            days = ''.join('1' if form.get(f"Days{i}{j}") == 'on' else '0'
                           for j in range(DAYS_PER_WEEK))
            try:
                name = self.snames_output_index[output - 1]
            except IndexError:
                name = existing.get('sprinkler_name') or key
            updated[key] = {
                'sprinkler_no': key,
                'sprinkler_name': name,
                'Arm': 1 if form.get(f"Arm{i}") == 'on' else 0,
                'Time': time_str,
                'Duration': duration,
                'Days': days,
                'Output': output,
            }
        return updated, errors

    def run(self):
        # Security: debug=False for production
        self.app.run(host='0.0.0.0', port=80, debug=False)

if __name__ == '__main__':
    server = HomeControlServer()
    server.run()
