"""Which nzfleet code runs, and when the daemon moves it. The decisions are plain functions; `Host` is the git, state and process side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

RULE = ("nzfleet runs from the checkout named by NZFLEET_CODE, which the daemon moves to main when main's ci is green; "
        "`nzfleet version` says what runs and how far behind it is.")
STALE_AFTER = 30 * 60
CANDIDATES = 5  # newest nzfleet commits whose ci is looked at in one pass


@dataclass(frozen=True)
class Behind:
    """A commit on main that changes tools/nzfleet and is not in the running code."""

    sha: str
    at: float


def target(behind: tuple, green: Callable[[str], bool]) -> Optional[str]:
    """The newest of `behind` (newest first) whose ci is green, or None."""
    return next((c.sha for c in behind if green(c.sha)), None)


def stale(behind: tuple, now: float) -> bool:
    return bool(behind) and now - min(c.at for c in behind) > STALE_AFTER


def distance(behind: tuple, now: float) -> str:
    if not behind:
        return "up to date with main"
    oldest = int((now - min(c.at for c in behind)) / 60)
    return f"behind main by {len(behind)} nzfleet commit{'s' if len(behind) != 1 else ''}, the oldest {oldest} min old"


def warning(behind: tuple, now: float) -> str:
    return f"nzfleet is {distance(behind, now)}; the daemon moves it when main's ci is green. See `nzfleet version`." if behind else ""


def attention(behind: tuple, now: float) -> str:
    return f"nzfleet code is {distance(behind, now)} (over {STALE_AFTER // 60} min)" if stale(behind, now) else ""


class Host(Protocol):
    def detached(self) -> bool: ...
    def head(self) -> str: ...
    def behind(self) -> tuple: ...
    def green(self, sha: str) -> bool: ...
    def refused(self) -> str: ...
    def move(self, sha: str) -> bool: ...
    def self_test(self) -> tuple: ...
    def refuse(self, sha: str) -> None: ...
    def moved(self, before: str, after: str) -> None: ...
    def tell(self, text: str) -> None: ...
    def install_units(self) -> None: ...
    def restart(self) -> None: ...


def advance(host: Host) -> str:
    """Moves the code to the newest green nzfleet commit on main, tests it there, and restarts on it.
    A failing test puts the old commit back and tells the owner once per target. Returns what happened, or ''."""
    if not host.detached():
        return ""
    sha = target(host.behind()[:CANDIDATES], host.green)
    if sha is None or sha == host.refused():
        return ""
    before = host.head()
    if not host.move(sha):
        return ""
    ok, why = host.self_test()
    if not ok:
        host.move(before)
        host.refuse(sha)
        host.tell(f"nzfleet stays on {before[:8]}: its own tests fail on {sha[:8]}.\n\n```\n{why}\n```")
        return f"not moving to {sha[:8]}: nzfleet's tests fail there"
    host.moved(before, sha)
    host.install_units()
    host.restart()
    return f"moved from {before[:8]} to {sha[:8]}"
