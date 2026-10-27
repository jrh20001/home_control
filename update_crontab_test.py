#!/usr/bin/python3
"""Legacy alias for the schedule builder.

The schedule is built by update_crontab.py; this file used to contain a second,
destructive copy of that logic (it wiped every `at` job).  It now delegates so
old crontabs or notes pointing at this name keep working safely.
"""

import sys

from update_crontab import main

if __name__ == '__main__':
    sys.exit(main())
