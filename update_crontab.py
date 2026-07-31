#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_crontab.py
Set up 'at' jobs for home control schedule based on config.json.
"""

import datetime
import os
import json
import logging
import subprocess
from suntime import Sun, SunTimeException

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='update_crontab.log',
    filemode='a'
)
logger = logging.getLogger(__name__)

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load config.json: {e}")
        raise

def get_time_offset(time_str):
    """Parse time offset string (e.g., '-00:30:00' or ' 22:29:00')"""
    parts = time_str.strip().split(':')
    sign = -1 if time_str.strip().startswith('-') else 1
    
    # Handle cases where the first part might have the sign attached
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2])
    
    # If the string was negative, hours will be negative (if parsed directly) 
    # or we need to apply sign. 
    # If parts[0] is '-00', int('-00') is 0, so we need the explicit sign check.
    
    return sign * abs(hours), sign * abs(minutes), abs(seconds)

def main():
    config = load_config()
    location = config.get('location', {})
    schedule = config.get('schedule', [])
    devices = config.get('devices', {})

    latitude = location.get('latitude', 37.37006)
    longitude = location.get('longitude', -122.08839)

    tdate = datetime.date.today()
    logger.info(f"Date: {tdate}")

    try:
        sun = Sun(latitude, longitude)
        sunrise = sun.get_local_sunrise_time(tdate)
        sunset = sun.get_local_sunset_time(tdate)
        logger.info(f"Sunrise: {sunrise.strftime('%H:%M:%S')}")
        logger.info(f"Sunset:  {sunset.strftime('%H:%M:%S')}")
    except SunTimeException as e:
        logger.error(f"Error calculating sun times: {e}")
        return

    # Base times
    today_midnight = datetime.datetime.combine(tdate, datetime.time.min)
    # Ensure sunrise/sunset are timezone-naive or compatible with today_midnight if needed
    # suntime returns timezone-aware datetimes. 
    # We'll convert today_midnight to be compatible or just use the hours/mins from sunrise/sunset.
    # Actually, let's keep everything as datetime objects.
    
    # Note: suntime returns UTC or local depending on usage, but get_local_... returns local time with tz info.
    # datetime.datetime.combine returns naive.
    # Let's make today_midnight aware if sunrise is aware.
    if sunrise.tzinfo:
        today_midnight = today_midnight.replace(tzinfo=sunrise.tzinfo)

    base_times = {
        'today': today_midnight,
        'sunrise': sunrise + datetime.timedelta(days=1),
        'sunset': sunset + datetime.timedelta(days=1),        
        'tomorrow': today_midnight + datetime.timedelta(days=1)
    }

    commands = []

    for item in schedule:
        base_time_key = item['time']
        offset_str = item['offset']
        device_key = item['device']
        action = item['action']

        if base_time_key not in base_times:
            logger.warning(f"Unknown base time '{base_time_key}', skipping.")
            continue

        base_time = base_times[base_time_key]
        
        # Parse offset
        # Format expected: [+-]HH:MM:SS
        # The original code logic was:
        # offt = slin[1].split(':')
        # sgn = -1 if offt[0][0] == '-' else 1
        # hrs = sgn * int(offt[0]) ...
        
        try:
            clean_offset = offset_str.strip()
            sign = -1 if clean_offset.startswith('-') else 1
            parts = clean_offset.replace('+', '').replace('-', '').split(':')
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            
            delta = datetime.timedelta(hours=hours, minutes=minutes)
            if sign == -1:
                target_time = base_time - delta
            else:
                target_time = base_time + delta
                
            # The original code also used 'seconds' as a sleep delay in the command, NOT part of the schedule time?
            # Original: cmd = 'sleep %d; echo rf %s %s ...' % (secs, dev, ...)
            # And: tim = stim.strftime('%H:%M')
            # So the 'at' job runs at HH:MM, and then sleeps for SS seconds.
            sleep_seconds = seconds

        except ValueError as e:
            logger.error(f"Invalid offset format '{offset_str}': {e}")
            continue

        device_info = devices.get(device_key)
        if not device_info:
            logger.warning(f"Device '{device_key}' not found in config, skipping.")
            continue

        # Construct command
        # Original: echo rf {dev} {action} | nc -N localhost 1099
        # We should use the full path or ensure environment is set, but 'nc' is standard.
        # We will keep the command string as is for 'at' to execute.
        
        # Note: The original script used 'dev' which was the device KEY (e.g. 'a1')?
        # Original: dlist = [['kitchen', 'a1'], ...] -> dev_dic.get('kitchen') -> 'a1'
        # So yes, it passes 'a1', 'a2', etc.
        
        cmd_str = f"sleep {sleep_seconds}; echo rf {device_key} {action} | nc -N localhost 1099"
        
        at_time = target_time.strftime('%H:%M')
        
        # Handle 'tomorrow' explicitly for 'at' command if needed, 
        # but 'at' usually takes HH:MM and assumes today unless it's passed.
        # If the calculated time is for tomorrow (e.g. base is tomorrow), we should pass 'tomorrow' to 'at'
        # or a full date. 'at' accepts 'HH:MM YYYY-MM-DD'.
        
        at_date_str = target_time.strftime('%Y-%m-%d')
        
        # We'll use the full date format for safety to avoid ambiguity
        commands.append({
            'cmd': cmd_str,
            'time': at_time,
            'date': at_date_str
        })

    # Clear existing 'at' queue
    logger.info("Clearing existing 'at' queue...")
    try:
        # Get list of job IDs
        result = subprocess.run(['atq'], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                job_id = line.split()[0]
                subprocess.run(['atrm', job_id], check=False)
    except Exception as e:
        logger.error(f"Failed to clear 'at' queue: {e}")

    # Schedule new jobs
    logger.info("Scheduling new jobs...")
    for item in commands:
        cmd = item['cmd']
        time_str = item['time']
        date_str = item['date']
        
        # echo "cmd" | at HH:MM YYYY-MM-DD
        full_at_cmd = ['at', time_str, date_str]
        
        try:
            # Pass the command to stdin of 'at'
            process = subprocess.run(
                full_at_cmd,
                input=cmd.encode(),
                capture_output=True,
                check=True
            )
            logger.info(f"Scheduled: {time_str} {date_str} -> {cmd}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to schedule job: {e.stderr.decode()}")

    logger.info("Schedule update complete.")

if __name__ == '__main__':
    main()
