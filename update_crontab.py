#!/usr/bin/python3
"""update_crontab.py — compile the config.json schedule into 'at' jobs.

Only home-control jobs (commands containing ``echo rf``) are removed; any other
``at`` job owned by the user is left alone.
"""

import datetime
import json
import logging
import os
import subprocess

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'update_crontab.log')
_LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
try:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT,
                        filename=LOG_PATH, filemode='a')
except OSError:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger(__name__)

try:
    from suntime import Sun, SunTimeException
except ImportError:                     # keep the module importable without suntime
    Sun = None                          # pyright: ignore[reportAssignmentType]
    SunTimeException = Exception        # pyright: ignore[reportAssignmentType]


JOB_MARKER = 'echo rf '                  # signature of a home-control at job
CMD_TIMEOUT = 60


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load config.json: {e}")
        raise


def get_time_offset(time_str):
    """Parse an offset string like '-00:30:00' into (hours, minutes, seconds)."""
    parts = time_str.strip().split(':')
    sign = -1 if time_str.strip().startswith('-') else 1
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2])
    return sign * abs(hours), sign * abs(minutes), abs(seconds)


def at_job_ids():
    """Job ids currently queued for this user."""
    try:
        result = subprocess.run(['atq'], capture_output=True, text=True,
                                timeout=CMD_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Failed to list 'at' queue: {e}")
        return []
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def at_job_command(job_id):
    try:
        result = subprocess.run(['at', '-c', job_id], capture_output=True,
                                text=True, timeout=CMD_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Failed to read at job {job_id}: {e}")
        return ''
    return result.stdout


def clear_home_control_jobs():
    """Remove only the at jobs this script created, never unrelated jobs."""
    removed = 0
    kept = 0
    for job_id in at_job_ids():
        if JOB_MARKER not in at_job_command(job_id):
            kept += 1
            continue
        try:
            subprocess.run(['atrm', job_id], timeout=CMD_TIMEOUT, check=False)
            removed += 1
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Failed to remove at job {job_id}: {e}")
    logger.info(f"Cleared {removed} home-control at job(s), kept {kept} unrelated job(s)")
    return removed


def sun_base_times(config):
    """Return the today/sunrise/sunset/tomorrow base datetimes (or None)."""
    location = config.get('location', {})
    latitude = location.get('latitude', 37.37006)
    longitude = location.get('longitude', -122.08839)
    tdate = datetime.date.today()
    try:
        if Sun is None:
            raise SunTimeException('suntime is not installed')
        sun = Sun(latitude, longitude)
        sunrise = sun.get_local_sunrise_time(tdate)
        sunset = sun.get_local_sunset_time(tdate)
    except SunTimeException as e:
        logger.error(f"Error calculating sun times: {e}")
        return None, None
    logger.info(f"Date: {tdate}")
    logger.info(f"Sunrise: {sunrise.strftime('%H:%M:%S')}")
    logger.info(f"Sunset:  {sunset.strftime('%H:%M:%S')}")

    today_midnight = datetime.datetime.combine(tdate, datetime.time.min)
    if sunrise.tzinfo:
        today_midnight = today_midnight.replace(tzinfo=sunrise.tzinfo)

    # sunrise/sunset are shifted one day: the daily run keeps the coming night's
    # jobs queued (a job generated today fires tomorrow at that clock time).
    base_times = {
        'today': today_midnight,
        'sunrise': sunrise + datetime.timedelta(days=1),
        'sunset': sunset + datetime.timedelta(days=1),
        'tomorrow': today_midnight + datetime.timedelta(days=1),
    }
    return base_times, today_midnight.tzinfo


def build_commands(schedule, devices, base_times, tzinfo):
    """Turn schedule entries into at job dicts; invalid/past entries are adjusted."""
    commands = []
    now = datetime.datetime.now(tzinfo)

    for item in schedule:
        base_time_key = item.get('time')
        offset_str = item.get('offset', '')
        device_key = item.get('device')
        action = item.get('action')

        if base_time_key not in base_times:
            logger.warning(f"Unknown base time '{base_time_key}', skipping.")
            continue
        if not devices.get(device_key):
            logger.warning(f"Device '{device_key}' not found in config, skipping.")
            continue

        try:
            clean_offset = offset_str.strip()
            sign = -1 if clean_offset.startswith('-') else 1
            parts = clean_offset.lstrip('+-').split(':')
            hours, minutes, seconds = (int(part) for part in parts)
            target_time = (base_times[base_time_key]
                           + sign * datetime.timedelta(hours=hours, minutes=minutes))
        except (ValueError, AttributeError):
            logger.error(f"Invalid offset format '{offset_str}' for {device_key}")
            continue

        if target_time <= now:
            # A 'today' entry saved after its time has passed used to make `at`
            # fail; run it tomorrow instead (the daily job rebuilds the queue).
            logger.warning(f"{device_key} {action} at {target_time.strftime('%H:%M')} "
                           "is in the past; scheduling it for tomorrow")
            target_time += datetime.timedelta(days=1)

        commands.append({
            'cmd': f"sleep {seconds}; echo rf {device_key} {action} "
                   "| nc -N localhost 1099",
            'time': target_time.strftime('%H:%M'),
            'date': target_time.strftime('%Y-%m-%d'),
        })
    return commands


def schedule_jobs(commands):
    """Queue every command with `at`; returns (scheduled, failed)."""
    scheduled = failed = 0
    for item in commands:
        try:
            subprocess.run(['at', item['time'], item['date']],
                           input=item['cmd'].encode(), capture_output=True,
                           timeout=CMD_TIMEOUT, check=True)
            scheduled += 1
            logger.info(f"Scheduled: {item['time']} {item['date']} -> {item['cmd']}")
        except subprocess.CalledProcessError as e:
            failed += 1
            stderr = (e.stderr or b'').decode(errors='replace').strip()
            logger.error(f"Failed to schedule {item['time']} {item['date']}: {stderr}")
        except (OSError, subprocess.SubprocessError) as e:
            failed += 1
            logger.error(f"Failed to schedule {item['time']} {item['date']}: {e}")
    return scheduled, failed


def main():
    config = load_config()
    base_times, tzinfo = sun_base_times(config)
    if base_times is None:
        return 1

    commands = build_commands(config.get('schedule', []),
                              config.get('devices', {}), base_times, tzinfo)
    clear_home_control_jobs()
    logger.info("Scheduling new jobs...")
    scheduled, failed = schedule_jobs(commands)
    logger.info(f"Schedule update complete: {scheduled} scheduled, {failed} failed")
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
