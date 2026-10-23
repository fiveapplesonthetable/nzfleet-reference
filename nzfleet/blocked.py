"""A daemon that cannot start agents for a reason that is not any task's fault says so in `nzfleet status` and in one email."""

from __future__ import annotations

import time
from typing import Mapping

from . import mail, state

LIVE = "NZFLEET_LIVE"  # set by bin/nzfleet and the service unit, so a test run cannot launch a real agent


def missing_licence(env: Mapping) -> str:
    """Why this process may not start agents, or '' when it may."""
    if env.get(LIVE):
        return ""
    return f"{LIVE} is not set (the installed nzfleet.service predates it; `nzfleet setup` reinstalls the unit)"


def line(blocked: dict) -> str:
    """The red line for `nzfleet status`; '' when nothing blocks."""
    if not blocked:
        return ""
    return f"daemon cannot start agents: {blocked['reason']}, since {time.strftime('%H:%M', time.localtime(blocked['since']))}"


def noted(blocked: dict, reason: str, now: float) -> dict:
    """The blocked record after `reason` was met: the first time it was met is kept while the reason stands."""
    if blocked and blocked["reason"] == reason:
        return blocked
    return {"reason": reason, "since": now, "mailed": False}


def note(reason: str) -> None:
    """Records the reason and emails the orchestrator once while it stands; a mail that failed is tried again."""
    with state.change() as s:
        s["blocked"] = noted(s["blocked"], reason, time.time())
        if s["blocked"]["mailed"]:
            return
        text = line(s["blocked"])
        s["blocked"]["mailed"] = mail.send("nzfleet: the daemon cannot start agents", text)


def clear() -> None:
    with state.change() as s:
        s["blocked"] = {}
