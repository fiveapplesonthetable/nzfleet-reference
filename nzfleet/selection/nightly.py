"""Whether a scheduled run is the one at the wanted hour in London.

GitHub schedules in UTC only, so a nightly workflow lists two crons an hour apart and the one whose
hour matches London's offset today is kept.
"""

from __future__ import annotations

import datetime
import zoneinfo

OFFSETS = {datetime.timedelta(hours=1): 1, datetime.timedelta(0): 0}


def london_offset(now: datetime.datetime) -> int | None:
    """London's UTC offset in hours at `now` (1 in summer, 0 in winter)."""
    return OFFSETS.get(now.astimezone(zoneinfo.ZoneInfo("Europe/London")).utcoffset())


def keeps(event: str, schedule: str, london_hour: int, now: datetime.datetime) -> bool:
    """Only a schedule is kept, and only the cron whose UTC hour is `london_hour` minus London's offset."""
    fields = schedule.split()
    offset = london_offset(now)
    if event != "schedule" or len(fields) < 2 or offset is None:
        return False
    return fields[1] == str(london_hour - offset)
