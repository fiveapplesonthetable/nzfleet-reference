"""Reads the machine for the supervisor: the process table and what a worktree shows of an agent's work. No decisions."""

from __future__ import annotations

import os
import re

from . import progress, supervise, world

TOOK = re.compile(r"\btook \d+s\b")
TAIL = 4096


def _read(path: str) -> str:
    """A /proc file's text, empty when it is gone or unreadable: a kernel thread has no command line."""
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def processes(proc: str = "/proc", ticks: int = 0, uptime: float = -1.0) -> list:
    """Every process as supervise.Proc: CPU seconds include children it has waited for, so a finished child still counts."""
    ticks = ticks or os.sysconf("SC_CLK_TCK")
    if uptime < 0:
        try:
            uptime = float(open(os.path.join(proc, "uptime")).read().split()[0])
        except (OSError, ValueError, IndexError):
            uptime = 0.0
    out = []
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return out
    for entry in entries:
        base = os.path.join(proc, entry)
        try:
            raw = open(os.path.join(base, "stat")).read()
            head, _, rest = raw.rpartition(")")
            name = head.partition("(")[2]
            f = rest.split()
            cwd = os.path.realpath(os.readlink(os.path.join(base, "cwd")))
            starved = b"NZFLEET_STARVE=1" in open(os.path.join(base, "environ"), "rb").read().split(b"\0")
            argv = tuple(a for a in _read(os.path.join(base, "cmdline")).split("\0") if a)
            cgroup = _read(os.path.join(base, "cgroup")).strip().rpartition("::")[2]
            out.append(supervise.Proc(int(entry), int(f[1]), int(f[2]), name, max(0.0, uptime - int(f[19]) / ticks),
                                      sum(int(x) for x in f[11:15]) / ticks, cwd, starved, argv, cgroup))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _mtime(path: str):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _last_test(path: str):
    """When the worktree last ran a check: the newest file `nzfleet prove` kept, or a run log with a `took Ns` line."""
    found = []
    for name in ("summary.json",):
        found.append(_mtime(os.path.join(path, ".nzproof", name)))
    try:
        logs = [os.path.join(path, ".nzrun", n) for n in os.listdir(os.path.join(path, ".nzrun")) if n.endswith(".log")]
    except OSError:
        logs = []
    for log in logs:
        try:
            with open(log, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - TAIL))
                if TOOK.search(f.read().decode(errors="replace")):
                    found.append(_mtime(log))
        except OSError:
            continue
    return max((t for t in found if t is not None), default=None)


def activity(path: str, base: str) -> progress.Activity:
    """Commits above `base`, files changed against it, and when a commit, an edit and a check last happened."""
    if not path or not os.path.isdir(path):
        return progress.Activity(0, 0, None, None, None)
    times = world.git("log", "--format=%ct", f"{base}..HEAD", cwd=path, check=False).split()
    changed = set(world.git("diff", "--no-renames", "--name-only", base, cwd=path, check=False).split()) | \
        set(world.git("ls-files", "--others", "--exclude-standard", cwd=path, check=False).split())
    edits = [t for t in (_mtime(os.path.join(path, f)) for f in changed) if t is not None]
    return progress.Activity(len(times), len(changed), float(times[0]) if times else None, max(edits, default=None), _last_test(path))


LOCK_FILES = (".cargo-lock", ".package-cache", "gradle.lock", ".lock")


def lock_holders(locks: str, inodes: set, inside: set) -> dict:
    """Who holds each lock on `inodes`, from /proc/locks text: inode -> pid, skipping holders in `inside` and the waiters."""
    out = {}
    for line in locks.splitlines():
        f = line.split()
        if "->" in f or len(f) < 6 or not f[4].isdigit():
            continue
        inode = f[5].rpartition(":")[2]
        if inode.isdigit() and int(inode) in inodes and int(f[4]) not in inside:
            out[int(inode)] = int(f[4])
    return out


def waits(table: list, root: int, proc: str = "/proc") -> dict:
    """What an agent's process tree is blocked on: the kernel wait names of its threads, and the command that holds a cargo or gradle lock it has open."""
    tree = {p.pid for p in supervise.below(table, root)}
    names, inodes = set(), set()
    for pid in tree:
        base = os.path.join(proc, str(pid))
        try:
            tasks = os.listdir(os.path.join(base, "task"))
        except OSError:
            tasks = []
        for task in tasks:
            wchan = _read(os.path.join(base, "task", task, "wchan")).strip()
            if wchan and wchan != "0":
                names.add(wchan)
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            fds = []
        for fd in fds:
            try:
                path = os.readlink(os.path.join(base, "fd", fd))
                if path.endswith(LOCK_FILES):
                    inodes.add(os.stat(os.path.join(base, "fd", fd)).st_ino)
            except OSError:
                continue
    holders = lock_holders(_read(os.path.join(proc, "locks")), inodes, tree)
    by_pid = {p.pid: p.name for p in table}
    return {"waiting_on": sorted(names), "lock_holder": next((by_pid.get(pid, "unknown") for pid in holders.values()), "")}
