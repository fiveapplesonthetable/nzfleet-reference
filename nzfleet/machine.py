"""The machine the checks run on: disk and load."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time

from . import config, cpu_budget
METER = cpu_budget.here()
LOW_DISK_GB = 20


# The CPU quota of each kind is on its slice (bin/nzfleet-*.slice), so every scope of a kind shares it.
# Docker containers they start run under the daemon's cgroup and are not covered.
SLICES = {"batch": "nzfleet-batch.slice", "agent": "nzfleet-agent.slice"}
LIMITS = {
    "batch": ["MemoryMax=48G"],
    "agent": ["MemoryHigh=12G", "MemoryMax=16G"],
}


def scoped(kind: str, argv: list, unit: str = "") -> list:
    """`argv` run in a systemd scope of its own in `kind`'s slice with its limits, or unchanged where there is no user manager.

    A named `unit` can be stopped later, which ends every process the command left behind.
    """
    if not shutil.which("systemd-run") or not os.environ.get("XDG_RUNTIME_DIR"):
        return argv
    props = [arg for p in LIMITS[kind] for arg in ("-p", p)]
    name = [f"--unit={unit}"] if unit else []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--slice={SLICES[kind]}", *name, *props, *argv]


def agent_unit(key: str, started: float) -> str:
    """The scope name for one start of `key`; the start time keeps a resume of the same key from reusing a live name."""
    return f"nzf-agent-{re.sub('[^A-Za-z0-9_-]', '-', key)}-{int(started)}"


def stop_unit(unit: str) -> None:
    """Stops the scope `unit`, killing whatever still runs in it. A scope that is already gone is not an error."""
    if shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR"):
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True, check=False)


def free_gb(path: str = "/") -> int:
    return shutil.disk_usage(path).free // 2**30


def load() -> float:
    return os.getloadavg()[0]


def cpu_percent() -> float | None:
    """The share of the fleet's CPU quota in use, or None where it cannot be measured."""
    return METER.sample(time.monotonic())


def busy() -> bool:
    for path in ("/srv/fleet", config.HOME, config.WORK, config.REPO, config.TARGETS):
        try:
            if free_gb(path) < LOW_DISK_GB:
                return True
        except OSError:
            continue
    # The quota is what throttles the fleet, so its use is the gate. Where there is no quota to read, the
    # load average stands in against the same share of the CPUs this process can use.
    used = cpu_percent()
    if used is not None:
        return used > config.MAX_CPU_PERCENT
    one, five, _ = os.getloadavg()
    return max(one, five) > config.usable_cpus() * config.MAX_CPU_PERCENT / 100


def reading(proc: str = "/proc", mounts: tuple | None = None) -> dict:
    """Load, cores, memory, disk per mount and the hottest sensor, each left out when this host does not say."""
    one, five, fifteen = os.getloadavg()
    out = {"load": [one, five, fifteen], "cores": config.usable_cpus(), "mem_total_gb": 0, "mem_free_gb": 0, "temp_c": None, "disk": []}
    try:
        kb = {k: int(v.split()[0]) for k, _, v in (line.partition(":") for line in open(os.path.join(proc, "meminfo")))}
        out["mem_total_gb"], out["mem_free_gb"] = kb["MemTotal"] // 2**20, kb["MemAvailable"] // 2**20
    except (OSError, ValueError, KeyError, IndexError):
        pass
    watched = mounts if mounts is not None else tuple(dict.fromkeys(("/", "/dev/shm", "/srv/fleet", config.HOME, config.WORK, config.REPO, config.TARGETS)))
    for mount in watched:
        try:
            out["disk"].append({"mount": mount, "free_gb": free_gb(mount)})
        except OSError:
            continue
    temps = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(open(zone).read()) / 1000)
        except (OSError, ValueError):
            continue
    out["temp_c"] = max(temps, default=None)
    return out
