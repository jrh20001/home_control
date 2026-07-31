#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_crontab.py
set up crontab for at reboot and at 3:30AM local to run home_control.py
which deletes current "at" queue and replace with new queue"""

import datetime, os
from suntime import Sun, SunTimeException

dlist = [
    ['kitchen',       'a1' ],
    ['dining',        'a2' ],
    ['living_south',  'a3' ],
    ['living_north',  'a4' ],
    ['family',        'a5' ],
    ['family_south',  'a15'],
    ['family_north',  'a16'],
    ['outdoor_front', 'a7' ],
    ['outdoor_back',  'a6' ],
    ['garage',        'a14'],
    ['renee_lamp',    'a11'],
    ['jerry_lamp',    'a12'],
    ['art_lamp',      'a13'],
]
dev_dic = dict(dlist)

slist = [
    ['sunset',    '-00:30:00',  'kitchen',        'on'],
    ['sunset',    '-00:30:10',  'dining',         'on'],
    ['sunset',    '-00:30:20',  'living_south',   'on'],
    ['sunset',    '-00:30:30',  'living_north',   'on'],
    ['sunset',    '-00:30:40',  'family',         'on'],
    ['sunset',    '-00:30:50',  'family_south',   'on'],
    ['sunset',    '-00:31:00',  'family_north',   'on'],
    ['sunset',    '-00:31:10',  'outdoor_front',  'on'],
    ['sunset',    '-00:31:20',  'outdoor_back',   'on'],
    ['sunset',    '-00:31:30',  'garage',         'on'],
    ['sunset',    '-00:31:40',  'renee_lamp',     'on'],
    ['sunset',    '-00:31:50',  'jerry_lamp',     'on'],
    ['sunset',    '-00:32:00',  'art_lamp',       'on'],
    ['today',     ' 22:29:00',  'kitchen',        'off'],
    ['today',     ' 22:29:10',  'dining',         'off'],
    ['today',     ' 22:29:20',  'living_south',   'off'],
    ['today',     ' 22:29:30',  'living_north',   'off'],
    ['tomorrow',  '  2:00:00',  'family',         'off'],
    ['today',     ' 23:59:20',  'family_south',   'off'],
    ['today',     ' 23:59:30',  'family_north',   'off'],
    ['today',     ' 23:00:00',  'outdoor_front',  'off'],
    ['today',     ' 22:15:10',  'outdoor_back',   'off'],
    ['today',     ' 23:30:00',  'garage',         'off'],
    ['today',     ' 22:30:10',  'renee_lamp',     'off'],
    ['today',     ' 22:30:20',  'jerry_lamp',     'off'],
    ['today',     ' 22:30:30',  'art_lamp',       'off'],
    ['today',     '  6:30:00',  'family',         'on' ],
    ['today',     '  8:08:00',  'family',         'off'],
]

tdate = datetime.date.today()
print(tdate.strftime('%Y %M %D'))
yr = int(tdate.strftime('%Y'))
mn = int(tdate.strftime('%m'))
dy = int(tdate.strftime('%d'))
today = datetime.datetime(yr, mn, dy, 0, 0, 0)

latitude  = +37.37006
longitude = -122.08839

sun = Sun(latitude, longitude)
sunrise = sun.get_local_sunrise_time(tdate)
sunset  = sun.get_local_sunset_time(tdate)
print('Sunrise %s' % sunrise.strftime('%H:%M:%S'))
print('Sunset  %s' % sunset.strftime('%H:%M:%S'))

stime_dic = dict([
    ['today',   today],
    ['sunrise', sunrise],
    ['sunset',  sunset],
])

# construct 'at' commands to set up schedule
cmds = []
tims = []
days = []
for i in range(len(slist)):
    slin = slist[i]
    offt = slin[1].split(':')
    sgn  = +1
    if offt[0][0] == '-':
        sgn = -1
    hrs  = sgn * int(offt[0])
    mins = sgn * int(offt[1])
    secs = int(offt[2])
    if slist[i][0] == 'tomorrow':
        stim = stime_dic.get('today') + \
               datetime.timedelta(hours = hrs, minutes = mins)
    else:
        stim = stime_dic.get(slin[0]) + \
               datetime.timedelta(hours = hrs, minutes = mins)
    dev  = dev_dic.get(slin[2])
    cmd  = 'sleep %d; echo rf %s %s | nc -N localhost 1099' \
            % (secs, dev, slin[3])
    tim  = stim.strftime('%H:%M')
    day  = ' '
    if slist[i][0] == 'tomorrow':
        day = 'tomorrow'
    cmds.append(cmd)
    tims.append(tim)
    days.append(day)
    #print(cmd, tim, day)

# delete 'at' queue
os.system('for i in $(atq | cut -f 1); do atrm $i; done')

# set commands in system 'at' queue
for i in range(len(cmds)):
    ocmd = 'echo "%s" | at %s %s' % (cmds[i], tims[i], days[i])
    print(ocmd)
#    os.system(ocmd)

print('at que is set up')

# echo "command_to_be_run" | at 09:00
