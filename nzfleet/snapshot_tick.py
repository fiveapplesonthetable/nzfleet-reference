"""Gathers what the snapshot needs from the host and writes it where `status` reads it. The decisions are in snapshot.py."""

from __future__ import annotations

import json
import os
import tempfile
import time

from . import agents, codehost, config, machine, observe, page, procs, serve, snapshot, state, transcripts, world

PATH = os.path.join(config.HOME, "snapshot.json")
ISSUE = os.path.join(config.HOME, "status-issue.json")
COSTS = os.path.join(config.HOME, "cost-cache.json")
PROJECTS = os.path.expanduser("~/.claude/projects")


def _json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_text(path: str, data: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def quotas(text: str) -> dict:
    """`batches=24,agents=12` as {owner: cores}; a malformed pair is left out."""
    out = {}
    for pair in text.split(","):
        name, _, cores = pair.partition("=")
        try:
            out[name.strip()] = float(cores)
        except ValueError:
            continue
    return out


def project_keys(worktrees: dict) -> dict:
    """Claude Code's project directory name -> the task key whose worktree it is."""
    return {transcripts.project_slug(w["path"]): w["agent"] for w in worktrees.values() if w.get("agent")}


def day_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _behind() -> tuple:
    try:
        return codehost.Checkout().behind()
    except (OSError, ValueError, world.Failed):
        return ()


def gather(now: float) -> dict:
    s = state.read()
    table = observe.processes()
    running = {k: a for k, a in s["agents"].items() if a["status"] == "running" and procs.alive(a["pid"])}
    activity = {k: observe.activity(path, agents.base_of(path, s))
                for k, a in running.items() for path in [a.get("worktree") or agents.worktree_of(k, a, s)]}
    cache = transcripts.scan(PROJECTS, transcripts.project_slug(config.REPO), project_keys(s["worktrees"]), _json(COSTS), now)
    _write(COSTS, cache)
    try:
        postsubmit_issue = world.issue("postsubmit repeats are red")
    except (world.Failed, OSError):
        postsubmit_issue = {"number": "?"}
    return {"now": now, "day_start": day_start(now), "state": s, "procs": table, "agent_pids": {k: a["pid"] for k, a in running.items()},
            "activity": activity, "machine": machine.reading(), "cost_records": transcripts.records(cache),
            "previous": _json(PATH), "quotas": quotas(config.CPU_QUOTAS), "repo": config.REPO, "behind": _behind(),
            "postsubmit_issue": postsubmit_issue,
            "secret_paths": tuple(p for p in (config.GCP_KEY, config.STATUS_TOKEN_FILE, config.NOTIFY) if p)}


def _write_page(shot: dict) -> None:
    _write_text(serve.PAGE, page.html(shot))


def _owner_issue(shot: dict) -> None:
    """Edits the one status issue in place. Its address line names the tailnet URL; without a tailnet it says so."""
    address = serve.find_address()
    url = f"http://{address}:{config.SERVE_PORT}/" if address else "not running (no Tailscale address)"
    body = page.issue_text(shot, url, tuple(p for p in (config.GCP_KEY, config.STATUS_TOKEN_FILE, config.NOTIFY) if p))
    last = _json(ISSUE)
    if not page.issue_due(body, last, shot["at"]):
        return
    world.set_issue(page.ISSUE_TITLE, body)
    _write(ISSUE, {"core": body.split("\n\n", 1)[1], "at": shot["at"]})


def write(dry_run: bool) -> None:
    if dry_run:
        return
    shot = snapshot.snapshot(gather(time.time()))
    _write(PATH, shot)
    _write_page(shot)
    _owner_issue(shot)
