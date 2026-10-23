"""When the phone journeys run after a merge, what they run, and which of their findings become issues, as pure functions of plain data."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from . import redact

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
REPORT_FROM = "medium"
# The run could not drive the app at all: nothing in the app is at fault.
NOT_THE_APP = ("JevError", "AdbError", "PermissionError", "LookupError", "URLError", "TimeoutError", "ConnectionError")


@dataclass(frozen=True)
class Settings:
    """Everything about the phone and the machine, from configuration; nothing here is a default for one phone."""

    serial: str
    package: str
    adb_socket: str
    key_file: str
    database_url: str
    port: int
    paths: tuple
    lock: str = ""


@dataclass(frozen=True)
class Decision:
    run: bool
    remember: bool
    why: str


def touches(changed: list, prefixes: tuple) -> bool:
    return any(path.startswith(prefix) for path in changed for prefix in prefixes)


def decide(settings: Settings, seen: str, head: str, changed: list, running: bool) -> Decision:
    """Whether main's move to `head` starts a run; `remember` says to record `head` as looked at either way."""
    if not settings.serial or not settings.key_file or not settings.database_url:
        return Decision(False, False, "no phone is configured")
    if running:
        return Decision(False, False, "a run is going")
    if seen == head:
        return Decision(False, False, "main has not moved")
    if not seen:
        return Decision(False, True, "first sight of main; nothing to compare with")
    if not touches(changed, settings.paths):
        return Decision(False, True, "the change does not touch the app or the API it reads")
    return Decision(True, True, "the change touches the app or the API it reads")


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple
    env: tuple
    cwd: str
    ok_codes: tuple = (0,)


def environment(settings: Settings, runs: str, lab: str, target: str) -> dict:
    """The environment of every step: the phone, the app and the key file by name, and a lab of this run's own."""
    return {
        "ADB_SERVER_SOCKET": settings.adb_socket,
        "DEVICE_LAB_ADB": settings.adb_socket,
        "DEVICE_LAB_SERIAL": settings.serial,
        "DEVICE_LAB_PACKAGE": settings.package,
        "DEVICE_LAB_PORT": str(settings.port),
        "DEVICE_LAB_DIR": lab,
        "NZELA_APP_PACKAGE": settings.package,
        "NZELA_DEVICE_LOCK_HELD": "1",
        "NZELA_JEV_KEY_FILE": settings.key_file,
        "JOURNEYS_RUNS_DIR": runs,
        "DATABASE_URL": settings.database_url,
        "CARGO_TARGET_DIR": target,
        "SQLX_OFFLINE": "true",
    }


def steps(settings: Settings, worktree: str, runs: str, lab: str, target: str) -> list:
    """Build main, start the lab, install the app on the one phone, drive it, take the lab down."""
    env = tuple(sorted(environment(settings, runs, lab, target).items()))
    lab_sh = posixpath.join(worktree, "scripts", "device-lab.sh")
    make = ("make", "-C", worktree)
    return [
        Step("build", ("cargo", "+1.98", "build", "--bin", "nzela", "--bin", "nzstandin"), env, worktree),
        Step("lab", (lab_sh, "up"), env, worktree),
        Step("install", (lab_sh, "devices"), env, worktree),
        Step("journeys", (*make, "journeys", f"SERIAL={settings.serial}"), env, worktree),
        Step("break-it", (*make, "break-it", f"SERIAL={settings.serial}"), env, worktree),
    ]


@dataclass(frozen=True)
class Finding:
    title: str
    severity: str
    detail: str
    run: str
    repro: str


def _repro(meta: dict, serial: str) -> str:
    kind, name = meta.get("kind"), meta.get("name", "")
    if kind == "scenario":
        return f"make break-it SERIAL={serial} SCENARIOS={name}"
    if kind == "journey":
        return f"make journeys SERIAL={serial} JOURNEY={name}"
    return f"make explore SERIAL={serial} PERSONA={name}"


def findings_of(run: str, summary: dict, meta: dict, serial: str) -> list:
    """The findings of one run's summary.json that are medium or worse, plus a journey or scenario that did not finish."""
    repro, found = _repro(meta, serial), []
    for item in summary.get("findings", []):
        if SEVERITY_RANK.get(item.get("severity"), 0) >= SEVERITY_RANK[REPORT_FROM]:
            found.append(Finding(item["title"], item["severity"], item.get("detail", ""), run, repro))
    error = summary.get("error", "")
    if summary.get("status") == "failed" and not found and not error.startswith(NOT_THE_APP):
        name = summary.get("name", run)
        found.append(Finding(f"{name} did not reach its goal", "medium", error or "no reason recorded", run, repro))
    return found


def issue_title(finding: Finding) -> str:
    return redact.text(f"Phone: {finding.title}")[:120]


def issue_body(finding: Finding, sha: str, directory: str, checks: list) -> str:
    """What a reader needs to see it again: the commit, the recording, the failed checks and the command."""
    failed = "\n".join(f"- {c['name']}: {c['detail']}" for c in checks if not c.get("passed"))
    text = (f"Found by the phone journeys after `{sha[:10]}` landed on main. Severity: {finding.severity}.\n\n"
            f"{finding.detail}\n\n"
            f"Recording and screens: `{directory}` (video.mp4, logcat.txt, the numbered screens).\n\n"
            f"Failed checks:\n{failed or '- none recorded'}\n\n"
            f"To repeat it on the phone:\n\n```\n{finding.repro}\n```\n")
    return redact.text(text)


def new_issues(found: list, is_open) -> list:
    """One finding per title, and none whose title an open issue already carries."""
    seen, out = set(), []
    for finding in found:
        title = issue_title(finding)
        if title not in seen and not is_open(title):
            out.append(finding)
        seen.add(title)
    return out


def teardown(worktree: str, env: tuple) -> Step:
    return Step("down", (posixpath.join(worktree, "scripts", "device-lab.sh"), "down"), env, worktree, ok_codes=(0, 1))
