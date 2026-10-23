"""Reading the event log: which lines are wanted, and when."""

from __future__ import annotations

import datetime
import re

STAMP = "%Y-%m-%d %H:%M:%S"
_AGO = re.compile(r"(\d+)([smhd])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(text: str, now: datetime.datetime) -> datetime.datetime:
    """`30m`, `2h`, `1d` before now, or a UTC date with an optional time. Raises ValueError on anything else."""
    ago = _AGO.match(text.strip())
    if ago:
        return now - datetime.timedelta(seconds=int(ago.group(1)) * _UNIT[ago.group(2)])
    for form in (STAMP, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text.strip(), form).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"cannot read {text!r} as a time; use 30m, 2h, 1d or 2026-10-10 17:00")


def select(lines: list, since: datetime.datetime | None, last: int) -> list:
    """Lines stamped at or after `since` (a line with no stamp belongs to the one before it), else the last `last`."""
    if since is None:
        return lines[-last:] if last > 0 else []
    wanted, keeping = [], False
    for line in lines:
        try:
            keeping = datetime.datetime.strptime(line[:19], STAMP).replace(tzinfo=datetime.timezone.utc) >= since
        except ValueError:
            pass
        if keeping:
            wanted.append(line)
    return wanted
