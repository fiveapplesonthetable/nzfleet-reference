"""How much of its CPU quota the fleet is using, read from cgroup v2."""

from __future__ import annotations

import os
from dataclasses import dataclass

WINDOW = 30.0  # seconds a usage delta must span before it is a reading


@dataclass(frozen=True)
class Budget:
    path: str
    cpus: float


def parse_cpus(cpu_max: str) -> float | None:
    """The CPUs a `cpu.max` line allows, or None when it sets no quota."""
    quota, _, period = cpu_max.strip().partition(" ")
    try:
        cpus = int(quota) / int(period)
    except ValueError:
        return None
    return cpus if cpus > 0 else None


def parse_usage(cpu_stat: str) -> int | None:
    """The `usage_usec` of a `cpu.stat` file."""
    for line in cpu_stat.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def percent(used_usec: int, seconds: float, cpus: float) -> float:
    """The share of `cpus` that `used_usec` of CPU time over `seconds` came to."""
    return 100.0 * used_usec / (seconds * 1_000_000 * cpus)


def find(root: str, own_cgroup: str) -> Budget | None:
    """The nearest ancestor of this process's cgroup that carries a CPU quota, or None where there is no cgroup v2."""
    line = next((entry for entry in own_cgroup.splitlines() if entry.startswith("0::")), None)
    if line is None:
        return None
    parts = [p for p in line[3:].split("/") if p]
    while parts:
        path = os.path.join(root, *parts)
        try:
            with open(os.path.join(path, "cpu.max")) as handle:
                cpus = parse_cpus(handle.read())
        except OSError:
            cpus = None
        if cpus is not None:
            return Budget(path, cpus)
        parts.pop()
    return None


def _read(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


class Meter:
    """Reports the budget's usage as a percentage of its quota, measured over at least WINDOW seconds."""

    def __init__(self, root: str, own_cgroup: str):
        self.root = root
        self.own = own_cgroup
        self.budget = None
        self.baseline: tuple[float, int] | None = None
        self.last: float | None = None

    def sample(self, now: float) -> float | None:
        """The latest percentage, or None until two samples WINDOW apart exist or where no quota is readable."""
        if self.budget is None:
            self.budget = find(self.root, self.own)
        if self.budget is None:
            return None
        used = parse_usage(_read(os.path.join(self.budget.path, "cpu.stat")))
        if used is None:
            return None
        if self.baseline is None:
            self.baseline = (now, used)
        elif now - self.baseline[0] >= WINDOW:
            self.last = percent(used - self.baseline[1], now - self.baseline[0], self.budget.cpus)
            self.baseline = (now, used)
        return self.last


def here() -> Meter:
    """A meter for this process's own cgroup tree."""
    return Meter("/sys/fs/cgroup", _read("/proc/self/cgroup"))
