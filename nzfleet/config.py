"""Where nzfleet finds things on the machine it runs on. All of it comes from the environment; nothing machine-specific is in the code."""

from __future__ import annotations

import math
import os
import re
import stat


FILE: dict = {}  # what the env file says, including values the environment overrode
_ENVIRONMENT = frozenset(os.environ)


def _entries(text: str) -> list[tuple[str, str]]:
    """Reads assignment values with systemd EnvironmentFile quoting and no expansion."""
    entries, offset = [], 0
    while offset < len(text):
        line_end = text.find("\n", offset)
        line_end = len(text) if line_end < 0 else line_end
        line = text[offset:line_end]
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in line:
            offset = line_end + 1
            continue
        equals = line.index("=")
        key = line[:equals].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            offset = line_end + 1
            continue
        start = offset + equals + 1
        while start < line_end and text[start] in " \t":
            start += 1
        if start < line_end and text[start] in "\"'":
            quote = text[start]
            chars, cursor = [], start + 1
            while cursor < len(text):
                char = text[cursor]
                if char == quote:
                    cursor += 1
                    break
                if quote == '"' and char == "\\" and cursor + 1 < len(text):
                    following = text[cursor + 1]
                    if following == "\n":
                        cursor += 2
                        continue
                    if following in ('"', "\\", "`", "$"):
                        chars.append(following)
                        cursor += 2
                        continue
                    chars.extend(("\\", following))
                    cursor += 2
                    continue
                chars.append(char)
                cursor += 1
            else:
                offset = len(text)
                continue
            tail_end = text.find("\n", cursor)
            offset = len(text) if tail_end < 0 else tail_end + 1
            if not text[cursor:tail_end if tail_end >= 0 else len(text)].strip():
                entries.append((key, "".join(chars)))
            continue
        chars, cursor = [], offset + equals + 1
        while cursor < len(text):
            char = text[cursor]
            if char == "\\" and cursor + 1 < len(text):
                following = text[cursor + 1]
                if following == "\n":
                    cursor += 2
                    continue
                chars.append(following)
                cursor += 2
                continue
            if char == "\n":
                break
            chars.append(char)
            cursor += 1
        entries.append((key, "".join(chars).strip(" \t\r")))
        offset = cursor + 1
    return entries


def _load(path: str) -> None:
    """The machine's env file, so a command typed by hand sees what the daemon sees. The environment wins."""
    try:
        with open(path) as source:
            entries = _entries(source.read())
    except OSError:
        return
    for key, value in entries:
        FILE[key] = value
        os.environ.setdefault(key, value)


_load(os.environ.get("NZFLEET_ENV", os.path.expanduser("~/.config/nzfleet/env")))
_ENVIRONMENT = frozenset(key for key in os.environ if key not in FILE or os.environ[key] != FILE[key])


def usable_cpus() -> int:
    """The CPUs this process can use, accounting for affinity and older platforms."""
    return len(available_cpus())


def available_cpus() -> list[int]:
    """The CPU IDs this process can use, accounting for affinity and older platforms."""
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return list(range(max(1, os.cpu_count() or 1)))


DEFAULT_CPU_PERCENT = 90.0


def cpu_limit(fallback: float | None = None) -> tuple[float, str | None]:
    """The percentage of the fleet's CPU quota above which no work starts, and any configuration problem.

    NZFLEET_MAX_LOAD, the setting this replaced, is still read when the percentage is not set: as a share of the CPUs.
    """
    name, configured = "NZFLEET_MAX_CPU_PERCENT", os.environ.get("NZFLEET_MAX_CPU_PERCENT")
    if configured is None and "NZFLEET_MAX_LOAD" in os.environ:
        name, configured = "NZFLEET_MAX_LOAD", os.environ["NZFLEET_MAX_LOAD"]
    if configured is None:
        return DEFAULT_CPU_PERCENT, None
    try:
        limit = float(configured)
    except ValueError:
        limit = math.nan
    legacy = name == "NZFLEET_MAX_LOAD"
    maximum = usable_cpus() * 2 if legacy else 100
    if math.isfinite(limit) and 0 <= limit <= maximum:
        return (min(100.0, limit / usable_cpus() * 100) if legacy else limit), None
    problem = (f"{name}={configured} must be a {'load' if legacy else 'percentage'} from 0 to {maximum}; "
               f"using {'the previous limit' if fallback is not None else 'the default'}")
    return (fallback if fallback is not None else DEFAULT_CPU_PERCENT), problem


def refresh(path: str | None = None) -> None:
    """Refreshes daemon controls from the env file while preserving process environment overrides."""
    global AGENT_SLOTS, MAX_CPU_PERCENT, CONFIG_PROBLEM
    source = path or os.environ.get("NZFLEET_ENV", os.path.expanduser("~/.config/nzfleet/env"))
    try:
        with open(source) as env_file:
            values = dict(_entries(env_file.read()))
    except OSError:
        values = {}
    controls = ("NZFLEET_AGENT_SLOTS", "NZFLEET_MAX_CPU_PERCENT", "NZFLEET_MAX_LOAD")
    for key in controls:
        if key in _ENVIRONMENT:
            continue
        if key in values:
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    AGENT_SLOTS = int(os.environ.get("NZFLEET_AGENT_SLOTS", "4"))
    MAX_CPU_PERCENT, CONFIG_PROBLEM = cpu_limit(fallback=MAX_CPU_PERCENT)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


HOME = _get("NZFLEET_HOME", os.path.expanduser("~/.nzfleet"))  # state, logs, runs and agent output


def private_tmpdir() -> str:
    """Returns a private temporary directory owned by this fleet installation."""
    path = os.path.abspath(os.path.join(HOME, "tmp"))
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"fleet temporary directory is not private: {path}")
    return path


def with_private_tmpdir(env: dict | None = None) -> dict:
    """Gives a child process the fleet's private temporary directory."""
    return {**(os.environ if env is None else env), "TMPDIR": private_tmpdir()}
REPO = _get("NZFLEET_REPO", os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
WORK = _get("NZFLEET_WORK", os.path.join(os.path.dirname(REPO), "work"))  # scratch worktrees
UPSTREAM = _get("NZFLEET_UPSTREAM", "nzela-org/nzela")
FORK = _get("NZFLEET_FORK", _get("NZFLEET_GIT_NAME"))  # GitHub login of the fork agents push to
OWNER = _get("NZFLEET_OWNER")  # GitHub login whose comments become agent tasks; empty turns that off
GIT_NAME, GIT_EMAIL = _get("NZFLEET_GIT_NAME"), _get("NZFLEET_GIT_EMAIL")
GCLOUD = _get("NZFLEET_GCLOUD", "gcloud")
GCP_KEY = _get("NZFLEET_GCP_KEY")  # a service-account key file, if gcloud is not already signed in
JEV_SHADOW = _get("NZFLEET_JEV_SHADOW") == "1"  # log what Jev would choose beside the rules that act
JEV_ACT = _get("NZFLEET_JEV_ACT", "1") != "0"  # enable only the per-point Jev actions whose rollout gates pass
GCP_PROJECT, GCP_REGION = _get("NZFLEET_GCP_PROJECT"), _get("NZFLEET_GCP_REGION")
STATUS_TOKEN_FILE = _get("NZFLEET_STATUS_TOKEN_FILE")  # a file holding a token that may write commit statuses; empty posts none
PREAMBLE = _get("NZFLEET_PREAMBLE")  # a file every agent brief starts with
AGENT = _get("NZFLEET_AGENT", "claude -p --model {model} --dangerously-skip-permissions {brief}")
AGENT_CODEX = _get("NZFLEET_AGENT_CODEX", "codex exec --model {model} {brief}")
AGENT_CLAUDE = _get("NZFLEET_AGENT_CLAUDE", "claude -p --model {model} --dangerously-skip-permissions {brief}")
CODEX_MODEL = _get("NZFLEET_CODEX_MODEL", "gpt-6-luna")
CPU_QUOTAS = _get("NZFLEET_CPU_QUOTAS")  # owner=cores pairs, comma separated, shown beside what each owner uses
WARM = _get("NZFLEET_WARM", os.path.join(HOME, "warm"))  # warm batch slots; empty turns them off
SCCACHE = _get("NZFLEET_SCCACHE", os.path.join(HOME, "sccache"))  # the Rust dependency cache every batch shares; empty turns it off
CACHE = _get("NZFLEET_CACHE", os.path.join(HOME, "cache"))  # cargo registry and npm tarballs, shared by every batch
TARGETS = _get("NZFLEET_TARGETS", "/dev/shm/nzt")  # cargo build output, in RAM: the disk under WORK is the bottleneck
BUILD_ROOTS = tuple(dict.fromkeys((TARGETS, _get("NZFLEET_TARGET", "/var/tmp/nzt"))))  # where build outputs live, for the tidy pass
OWNER_ISSUE = int(_get("NZFLEET_OWNER_ISSUE", "695"))  # the issue a task that cannot finish is reported on
NOTIFY = _get("NZFLEET_NOTIFY")  # notify.py, which emails the owner and no one else; empty sends nothing
PSQL = _get("NZFLEET_PSQL", "psql")
TOOLCHAIN = {k: FILE[k] for k in ("PATH", "CARGO_HOME", "RUSTUP_HOME") if k in FILE}  # what a worktree's shell needs to find cargo
ANDROID_HOME = _get("ANDROID_HOME")
GRADLE_SLOT = _get("NZFLEET_GRADLE_SLOT")  # a wrapper that waits for one of the machine's Android build slots
AGENT_SLOTS = int(_get("NZFLEET_AGENT_SLOTS", "4"))  # agents running at once; 0 pauses new starts
BATCH_WALL_CLOCK = int(_get("NZFLEET_BATCH_WALL_CLOCK", str(8 * 3600)))
# No new work above this share of the fleet's CPU quota: past it the console and Android suites start timing out.
MAX_CPU_PERCENT, CONFIG_PROBLEM = cpu_limit()
SKIP_AREAS = frozenset(a for a in _get("NZFLEET_SKIP_AREAS").split(",") if a)  # proof inputs still require formal
GENERATORS = _get("NZFLEET_GENERATORS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generators.json"))  # path -> command that draws that file
JOURNEYS_SERIAL = _get("NZFLEET_JOURNEYS_SERIAL")  # the phone the journeys run on after a merge; empty turns them off
JOURNEYS_PACKAGE = _get("NZFLEET_JOURNEYS_PACKAGE", "app.nzela")  # the app on that phone, the only package they touch
JOURNEYS_ADB = _get("NZFLEET_JOURNEYS_ADB")  # the adb server the phone is on, as tcp:<host>:<port>
JOURNEYS_KEY_FILE = _get("NZFLEET_JOURNEYS_KEY_FILE")  # the file holding the Jev key
JOURNEYS_DATABASE_URL = _get("NZFLEET_JOURNEYS_DATABASE_URL")  # a Postgres whose login may create databases, for the run's lab
JOURNEYS_PORT = int(_get("NZFLEET_JOURNEYS_PORT", "8080"))  # the lab's first port, of four; the phones' host forwards 8080
JOURNEYS_LOCK = _get("NZFLEET_JOURNEYS_LOCK", "/tmp/nzela-device.lock")  # the lock every job that uses a phone or the lab ports takes
JOURNEYS_PATHS = tuple(p for p in _get("NZFLEET_JOURNEYS_PATHS", "frontend/android/,crates/nzela/src/api/").split(",") if p)  # a change under these starts a run
SERVE_PORT = int(_get("NZFLEET_SERVE_PORT", "8787"))  # the port `nzfleet serve` listens on, on the Tailscale address only
