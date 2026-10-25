"""The check that someone hears when the daemon stops or stops completing ticks."""

from __future__ import annotations

import os
import subprocess
import time

from . import machine, plan, rootdisk, tick, world

UNIT = "nzfleet"
HEARTBEAT = os.path.join(os.path.dirname(tick.LOG), "daemon.heartbeat")


def _active() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def _log() -> tuple:
    """Seconds since tick.log was written (None if absent) and its last 20 lines."""
    try:
        with open(tick.LOG) as f:
            lines = f.read().splitlines()[-20:]
        return time.time() - os.path.getmtime(tick.LOG), lines
    except OSError:
        return None, []


def heartbeat() -> None:
    """Record that the daemon completed a tick, including one with no log output."""
    os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
    with open(HEARTBEAT, "w"):
        os.utime(HEARTBEAT)


def _heartbeat_age() -> float | None:
    try:
        return time.time() - os.path.getmtime(HEARTBEAT)
    except OSError:
        return None


def root_disk(usage=machine.root_usage) -> str:
    """Keeps one open issue while `/` is past the warning line, and closes it when it is not."""
    warning = rootdisk.warning(*usage())
    found = world.issue(rootdisk.TITLE)
    if warning and (found is None or found["body"] != warning):
        world.set_issue(rootdisk.TITLE, warning)
    elif not warning and found:
        world.close_issue(found["number"])
    return f"root disk: {warning or 'comfortable'}"


def check() -> str:
    _, tail = _log()
    active = _active()
    age = _heartbeat_age()
    if not active:
        reason = "the daemon is not active"
    elif age is None:
        reason = "the daemon heartbeat does not exist"
    elif age > plan.SILENT_AFTER:
        reason = f"the daemon heartbeat has not advanced for {int(age // 60)} minutes"
    else:
        reason = None
    alert = plan.down_alert(reason, tail, world.issue(plan.DOWN_TITLE))
    if alert.kind in (plan.Alarm.OPEN, plan.Alarm.UPDATE):
        world.set_issue(plan.DOWN_TITLE, alert.body)
    elif alert.kind is plan.Alarm.CLOSE:
        world.close_issue(alert.number)
    return f"{alert.kind.value}: {reason or 'healthy'}"
