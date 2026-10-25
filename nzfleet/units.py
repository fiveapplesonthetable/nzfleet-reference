"""The systemd units nzfleet ships in bin/ against the ones installed for the user; a unit that differs is reinstalled."""

from __future__ import annotations

import os
import subprocess

from . import tmpdirs

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
SERVICE = "nzfleet.service"
RUNNER_DROPIN = os.path.join("nzela-runner@.service.d", "50-tmp.conf")
MIRROR_DROPIN = os.path.join("nzela-runner@.service.d", "60-git-mirror.conf")
UNITS = (SERVICE, "nzfleet-serve.service", "nzfleet-watchdog.service", "nzfleet-watchdog.timer", "nzfleet-batch.slice", "nzfleet-agent.slice", "nzfleet-background.slice")
# The owner's standing services that belong under nzfleet-background.slice. The ffmpeg and scrcpy
# processes of the capture are children of phone-capture.service and follow it into the slice.
BACKGROUND_UNITS = (
    "phone-capture.service", "phone-capture-direct-gateway.service", "phone-viewer-review.service",
    "recordings-mediamtx.service", "recordings-viewer.service", "recordings-phone-activity.service",
)
BACKGROUND_DROPIN = "50-nzfleet-background.conf"
BACKGROUND_TEXT = "[Service]\nSlice=nzfleet-background.slice\n"


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


def sync_runner_tmp(home: str, root: str, reload=_reload) -> bool:
    """Points every CI runner instance at its own temporary directory under `root`. True when the drop-in changed."""
    path = os.path.join(installed_dir(home), RUNNER_DROPIN)
    text = tmpdirs.runner_dropin(root)
    if _read(path) == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    reload()
    return True


def mirror_dropin(token_file: str = "") -> str:
    """A systemd drop-in naming the account's git mirror, which the job-started hook refreshes and `select` borrows."""
    lines = ["[Service]", "Environment=NZELA_GIT_MIRROR=%h/git-mirror/repo.git"]
    if token_file:
        lines.append(f"Environment=NZELA_GIT_MIRROR_TOKEN_FILE={token_file}")
    return "\n".join(lines) + "\n"


def sync_runner_hook(home: str, hook: str, token_file: str = "", reload=_reload) -> tuple:
    """Installs the job-started hook beside the runners and the drop-in naming the mirror it refreshes.

    A runner account has `actions-runner` under its home; any other account gets nothing. Returns what was written."""
    runners = os.path.join(home, "actions-runner")
    text = _read(hook)
    if not os.path.isdir(runners) or text is None:
        return ()
    written = []
    installed = os.path.join(runners, "job-started.sh")
    if _read(installed) != text:
        with open(installed, "w") as f:
            f.write(text)
        os.chmod(installed, 0o755)
        written.append(installed)
    dropin = os.path.join(installed_dir(home), MIRROR_DROPIN)
    wanted = mirror_dropin(token_file)
    if _read(dropin) != wanted:
        os.makedirs(os.path.dirname(dropin), exist_ok=True)
        with open(dropin, "w") as f:
            f.write(wanted)
        written.append(dropin)
        reload()
    return tuple(written)

def sync_background(home: str, reload=_reload) -> tuple:
    """Writes the slice drop-in of each background unit installed for the user. Returns the units changed.

    A drop-in is read at the unit's next start; nothing here restarts a running service.
    """
    target = installed_dir(home)
    changed = []
    for unit in BACKGROUND_UNITS:
        if _read(os.path.join(target, unit)) is None:
            continue
        path = os.path.join(target, unit + ".d", BACKGROUND_DROPIN)
        if _read(path) == BACKGROUND_TEXT:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(BACKGROUND_TEXT)
        changed.append(unit)
    if changed:
        reload()
    return tuple(changed)
