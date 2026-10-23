"""What a batch log may say to a decider off this machine: its last lines scrubbed, and who held the locks it waited on."""

from __future__ import annotations

import os
import re
from typing import Callable

from . import redact

TAIL_LINES = 60
LINE_CHARS = 240
WAIT = re.compile(r"Blocking waiting for file lock on ([\w ]+)")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+")
_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|dev|app|internal|local|lan|ts\.net)\b", re.I)
_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d ()-]{8,}\d(?![\w.])")
_PATH = re.compile(r"(?<![\w.:])~?/(?:[\w.@+-]+/)+([\w.@+-]*)")
HOLDERS = frozenset({"cargo", "rustc", "rustup", "docker", "npm", "node", "gradle", "java", "sccache"})
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def scrub(line: str) -> str:
    """One log line with credentials, URLs, hosts, addresses, phone numbers and directory prefixes taken out; file names stay."""
    line = _ANSI.sub("", redact.text(line))
    for pattern, mark in ((_URL, "<url>"), (_HOST, "<host>"), (_ADDRESS, "<address>"), (_PHONE, "<number>")):
        line = pattern.sub(mark, line)
    return _PATH.sub(lambda m: "<path>/" + m.group(1), line)[:LINE_CHARS]


def tail(log: str, lines: int = TAIL_LINES) -> list[str]:
    """The last `lines` non-empty lines of `log`, scrubbed."""
    return [scrub(line) for line in [l for l in log.splitlines() if l.strip()][-lines:]]


def lock_waits(log: str) -> list[str]:
    """The kinds of cargo lock the log says it waited for, once each."""
    return sorted({m.group(1).strip() for m in WAIT.finditer(log)})


def lock_holders(proc_locks: str, comm: Callable[[int], str]) -> list[str]:
    """The names, once each, of build tools holding a write lock according to /proc/locks; pids stay on the machine."""
    names = set()
    for row in proc_locks.splitlines():
        fields = row.split()
        if len(fields) > 5 and fields[1] in ("FLOCK", "POSIX") and fields[3] == "WRITE" and fields[4].isdigit():
            name = comm(int(fields[4]))
            if name in HOLDERS:
                names.add(name)
    return sorted(names)




def comm_of(pid: int, proc: str = "/proc") -> str:
    try:
        with open(os.path.join(proc, str(pid), "comm"), encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def holders_now(proc: str = "/proc") -> list[str]:
    """The lock holders on this machine now."""
    try:
        with open(os.path.join(proc, "locks"), encoding="utf-8", errors="replace") as f:
            return lock_holders(f.read(), lambda pid: comm_of(pid, proc))
    except OSError:
        return []
