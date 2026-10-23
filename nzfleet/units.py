"""The systemd units nzfleet ships in bin/ against the ones installed for the user; a unit that differs is reinstalled."""

from __future__ import annotations

import os
import subprocess

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
SERVICE = "nzfleet.service"
UNITS = (SERVICE, "nzfleet-serve.service", "nzfleet-watchdog.service", "nzfleet-watchdog.timer", "nzfleet-batch.slice", "nzfleet-agent.slice")


def drifted(shipped: dict, installed: dict) -> tuple:
    """Names whose installed text is missing or differs from the shipped text, in shipped order."""
    return tuple(name for name, text in shipped.items() if installed.get(name) != text)


def installed_dir(home: str) -> str:
    return os.path.join(home, ".config", "systemd", "user")


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _reload() -> None:
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def sync(home: str = os.path.expanduser("~"), shipped_from: str = BIN, reload=_reload) -> tuple:
    """Writes every shipped unit that differs from the installed one and reloads systemd. Returns the names written."""
    shipped = {u: text for u in UNITS if (text := _read(os.path.join(shipped_from, u))) is not None}
    target = installed_dir(home)
    changed = drifted(shipped, {u: _read(os.path.join(target, u)) for u in shipped})
    if not changed:
        return ()
    os.makedirs(target, exist_ok=True)
    for name in changed:
        with open(os.path.join(target, name), "w") as f:
            f.write(shipped[name])
    reload()
    return changed
