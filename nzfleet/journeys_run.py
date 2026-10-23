"""Starts the phone journeys when main moves and carries one run out. What to run and what to report are in journeys_plan.py."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from . import config, journeys_plan as plan, procs, state, world

SUMMARY, META, STEP_LIMIT = "summary.json", "meta.json", 3 * 3600


def settings() -> plan.Settings:
    return plan.Settings(config.JOURNEYS_SERIAL, config.JOURNEYS_PACKAGE, config.JOURNEYS_ADB, config.JOURNEYS_KEY_FILE,
                         config.JOURNEYS_DATABASE_URL, config.JOURNEYS_PORT, config.JOURNEYS_PATHS, config.JOURNEYS_LOCK)


def _changed(old: str, new: str) -> list:
    return world.git("diff", "--no-renames", "--name-only", f"{old}..{new}").split()


def _spawn(sha: str) -> int:
    code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-m", "nzfleet", "journeys", "run", sha], cwd=code, env=config.with_private_tmpdir(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def _run_step(step: plan.Step, log) -> int:
    env = {**config.with_private_tmpdir(), **config.TOOLCHAIN, **dict(step.env)}
    try:
        return subprocess.run(step.argv, cwd=step.cwd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=STEP_LIMIT).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        log.write(f"{step.name}: {type(e).__name__}\n")
        return 124


@contextlib.contextmanager
def _hold(path: str):
    """The machine's device lock, waited for: another job may have the phone."""
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _issue_open(title: str) -> bool:
    return world.issue(title) is not None


@dataclass
class Host:
    """What the job asks of the machine; the defaults are the real one."""
    head: Callable = world.main_sha
    changed: Callable = _changed
    alive: Callable = procs.alive
    spawn: Callable = _spawn
    run_step: Callable = _run_step
    git: Callable = world.git
    issue_open: Callable = _issue_open
    create_issue: Callable = world.set_issue
    hold: Callable = _hold
    now: Callable = time.time


def tick(host: Host, tell: Callable, dry_run: bool = False) -> None:
    """Once per tick: when main has moved onto the app or the API it reads, start one run, unless one is going."""
    chosen = settings()
    record = state.read()["journeys"]
    run = record.get("run") or {}
    seen, head = record.get("seen", ""), host.head()
    changed = host.changed(seen, head) if seen and seen != head else []
    decision = plan.decide(chosen, seen, head, changed, bool(run.get("pid")) and host.alive(run["pid"]))
    if dry_run or (not decision.run and not decision.remember):
        return
    pid = host.spawn(head) if decision.run else 0
    with state.change() as st:
        st["journeys"] = {**st["journeys"], "seen": head}
        if decision.run:
            st["journeys"]["run"] = {"sha": head, "pid": pid, "started": host.now()}
    if decision.run:
        tell(f"journeys: main moved to {head[:8]} and touches the app; running them on the phone")


def reported(directory: str, sha: str, serial: str, host: Host) -> list:
    """Creates an issue for each new finding in the run directories under `directory`; returns the issue numbers."""
    found, where = [], {}
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        path = os.path.join(directory, name)
        summary = _read(os.path.join(path, SUMMARY))
        for finding in plan.findings_of(name, summary, _read(os.path.join(path, META)), serial):
            found.append(finding)
            where[finding.run] = (path, summary.get("checks", []))
    numbers = []
    for finding in plan.new_issues(found, host.issue_open):
        path, checks = where[finding.run]
        numbers.append(host.create_issue(plan.issue_title(finding), plan.issue_body(finding, sha, path, checks)))
    return numbers


def run(host: Host, tell: Callable, sha: str) -> int:
    """The detached body: build main, then with the phone to ourselves install it, drive it, report, and clean up."""
    chosen = settings()
    base = os.path.join(config.HOME, "journeys", sha[:10])
    runs, lab = os.path.join(base, "runs"), os.path.join(base, "lab")
    worktree, target = os.path.join(config.WORK, f"journeys-{sha[:10]}"), os.path.join(config.TARGETS, f"journeys-{sha[:10]}")
    os.makedirs(runs, exist_ok=True)
    host.git("worktree", "add", "--detach", worktree, sha)
    build, *driven = plan.steps(chosen, worktree, runs, lab, target)
    codes = {}
    try:
        with open(os.path.join(base, "steps.log"), "w") as log:
            codes[build.name] = host.run_step(build, log)
            if codes[build.name] == 0:
                # The lab binds ports the phones' host forwards, so the lock is held from `lab` to `down`, not only while driving.
                with host.hold(chosen.lock):
                    for step in driven:
                        codes[step.name] = host.run_step(step, log)
                        if step.name in ("lab", "install") and codes[step.name] != 0:
                            break
                    host.run_step(plan.teardown(worktree, build.env), log)
    finally:
        host.git("worktree", "remove", "--force", worktree)
        shutil.rmtree(target, ignore_errors=True)
    numbers = reported(runs, sha, chosen.serial, host)
    produced = len(os.listdir(runs))
    with state.change() as st:
        st["journeys"]["run"] = {}
        st["journeys"]["last"] = {"sha": sha, "at": host.now(), "steps": codes, "runs": produced, "issues": numbers}
    broke = [name for name in ("build", "lab", "install") if codes.get(name, 0) != 0]
    silent = [name for name in ("journeys", "break-it") if codes.get(name, 0) != 0 and not produced]
    head = f"journeys on {sha[:8]}: "
    tell(head + (f"{broke[0]} failed, see {base}/steps.log" if broke else
                 f"{', '.join(silent)} exited non-zero and left no run, see {base}/steps.log" if silent else
                 f"{produced} run(s), {len(numbers)} new issue(s)"))
    return 1 if broke or silent else 0
