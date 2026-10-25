"""Headless Claude agents for work a person would otherwise pick up: a PR comment, a replay that needs hands, a red main."""

from __future__ import annotations

import os
import subprocess
import time

from . import blocked, config, dispatch, machine, plan, procs, rebases, runs, selfupdate, state, task_queue, world

from .config import AGENT as COMMAND, AGENT_CLAUDE, AGENT_CODEX, FORK, PREAMBLE, REPO, WORK
LOGS = os.path.join(state.HOME, "agents")
HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "no-background-bash.py")
LIMIT = 3 * 3600


def selection(profile: dict, model: str, provider_override: str | None, pinned: bool = False) -> tuple[str, str, str]:
    """Resolves a task's own choice first, then the active session, then a caller selection, then machine configuration."""
    if pinned and provider_override:
        provider, selected_model = provider_override, model
    elif profile:
        provider = profile.get("provider", "configured")
        selected_model = profile.get("model", model)
    else:
        provider = provider_override or "configured"
        selected_model = model
    command = {"codex": AGENT_CODEX, "claude": AGENT_CLAUDE}.get(provider, COMMAND)
    return provider, selected_model, command


FIX_KEYS = ("fix-", "main-", "hands-")
FIX_RULES = ("Rules from the owner for this run. A red check is red for a reason: find it and fix it. Never lengthen a timeout, "
             "add a retry, force serial mode or raise a size budget to make a check pass; a pull request that only does that "
             "is wrong, and two were opened that way on 2026-10-09 (#1036, #1042). If you cannot find the cause, report what "
             "you found and stop without pushing. Before every push run `tools/nzfleet/bin/nzfleet-guard limits`; it refuses such a diff.")


def brief(task: str, key: str = "") -> str:
    first = f"Read {PREAMBLE} and follow it exactly. Then do" if PREAMBLE else "Do"
    rules = f"\n\n{FIX_RULES}" if runs.base_key(key).startswith(FIX_KEYS) else ""
    return (f"{first} this task in the repository at {REPO}, and finish with at most 10 lines saying what you did "
            f"and the PR number.\n\n{task}{rules}\n\n{selfupdate.RULE}\n\n{plan.NOTE_INSTRUCTION.format(key=runs.base_key(key))}")


def refuse(key: str, why: str) -> str:
    """Records that `key` was not dispatched. Returns the sentence the first time, an empty string while the reason stands."""
    with state.change() as s:
        if s["refused"].get(key, {}).get("reason") == why:
            return ""
        s["refused"][key] = {"reason": why, "at": time.time()}
    return f"not starting {key}: {why}"


def start(key: str, task: str, model: str = "sonnet", dry_run: bool = False, area: str = "", worktree: str = "",
          pr: int | None = None, resume_session: str = "", provider_override: str | None = None, pinned: bool = False) -> str:
    """Starts an agent for `key` unless one is already running or finished for it. Returns what happened.

    A task that names a PR is refused when the PR is not open, its branch or head is gone, or the key already ran at that head.
    A `resume_session` continues that Claude session with `task` as the next message instead of a fresh brief.
    """
    current = state.read()
    if refusal := runs.dispatch_refusal(key, current["holds"], config.AGENT_SLOTS):
        return f"not starting {key}: {refusal}"
    if dry_run:
        known = current.get("agents", {}).get(key)
        if known and (known["status"] == "running" or known.get("task") == task):
            return ""
        profile = current.get("agent_session", {})
        selected_provider, selected_model, command = selection(profile, model, provider_override, pinned)
        return f"would start a {dispatch.provider_name(command, selected_provider)} {selected_model} agent for {key}"
    why = blocked.missing_licence(os.environ)
    if why:
        blocked.note(why)
        return f"not starting an agent for {key}: {why}"
    head = ""
    if pr is not None:
        try:
            facts = world.pull_facts(pr)
        except world.Failed as e:
            return f"not starting {key}: could not check #{pr}: {e}"
        why = dispatch.refusal(pr, facts)
        if why:
            return refuse(key, why)
        head = facts.head
    with state.change() as s:
        if refusal := runs.dispatch_refusal(key, s["holds"], config.AGENT_SLOTS):
            return f"not starting {key}: {refusal}"
        agents = s.setdefault("agents", {})
        known = agents.get(key)
        if known and (known["status"] == "running" or known.get("task") == task):
            return ""
        if runs.attempt(key) == 1 and dispatch.already_ran(key, head, agents):
            return ""
        s["refused"].pop(key, None)
        os.makedirs(LOGS, exist_ok=True)
        log = os.path.join(LOGS, f"{key.replace('/', '-')}-{int(time.time())}.log")
        profile = s.get("agent_session", {})
        selected_provider, selected_model, command = selection(profile, model, provider_override, pinned)
        provider = dispatch.provider_name(command, selected_provider)
        session = resume_session or (dispatch.new_session() if dispatch.has_sessions(command) else "")
        prompt = task if resume_session else brief(task, key)
        settings = os.path.join(state.HOME, "agent-settings.json")
        with open(settings, "w") as output:
            output.write(dispatch.settings(HOOK))
        argv = dispatch.with_settings(dispatch.command_argv(command, selected_model, prompt), settings)
        argv = dispatch.with_session(argv, session, bool(resume_session))
        started = time.time()
        unit = machine.agent_unit(key, started)
        with open(log, "w") as output:
            proc = subprocess.Popen(
                machine.scoped("agent", ["timeout", str(LIMIT), *argv], unit=unit),
                cwd=REPO, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                env={**config.with_agent_tmpdir(runs.base_key(key)), "NZFLEET_AGENT_KEY": runs.base_key(key)})
        agents[key] = {"status": "running", "pid": proc.pid, "task": task, "provider": provider,
                       "provider_selection": selected_provider, "model": selected_model, "log": log, "started": started, "unit": unit,
                       "process_start": _process_start(proc.pid),
                       "area": area, "worktree": worktree, "head": head, "session": session, "for_pr": pr,
                       **({"pinned": True} if pinned else {})}
        s["blocked"] = {}
        return f"started a {provider} {selected_model} agent for {key}"


def reap() -> list:
    """Marks agents whose process ended as done, or as unfinished when their log shows no summary, with the last lines they wrote."""
    finished, units = [], []
    with state.change() as s:
        for key, agent in s.setdefault("agents", {}).items():
            if agent["status"] != "running" or _alive(agent["pid"], agent.get("process_start")):
                continue
            agent["finished"] = time.time()
            units.append(agent.get("unit"))
            try:
                with open(agent["log"]) as f:
                    log = f.read()
            except OSError:
                log = ""
            agent["summary"] = log[-1500:]
            agent["status"] = "unfinished" if runs.unfinished(log) else "done"
            agent["worktree"] = agent.get("worktree") or worktree_of(key, agent, s, guess=True)
            finished.append(key)
    for unit in filter(None, units):
        machine.stop_unit(unit)
    return finished


def record_prs() -> list:
    """Stores `pr` on each finished run that has none, and how it was learned: the number the agent stated, else a guess.

    A run that stated `PR: none` stores no PR and says so. A failed lookup leaves the run unrecorded so the next tick tries again.
    """
    agents = state.read().get("agents", {})
    todo = {k: a for k, a in agents.items() if a["status"] == "done" and "pr" not in a}
    if not todo:
        return []
    prs = None
    found = {}
    for key, a in todo.items():
        try:
            log = open(a["log"], errors="replace").read()
        except OSError:
            log = ""
        stated = plan.stated_pr(log)
        if stated == "none":
            found[key] = (None, True)
        elif stated is not None:
            try:
                found[key] = ({"number": stated, "branch": world.branch_of(stated), "how": "stated"}, False)
            except world.Failed:
                continue
        else:
            prs = world.fork_prs() if prs is None else prs
            guess = plan.find_pr(world.worktree_branches_between(a["started"], a.get("finished", a["started"])), log, prs, a["started"], FORK)
            found[key] = (guess and {**guess, "how": "guessed"}, False)
    with state.change() as s:
        for key, (pr, stated_none) in found.items():
            s["agents"][key]["pr"] = pr
            if stated_none:
                s["agents"][key]["stated_no_pr"] = True
    return [f"{key} -> " + (f"#{pr['number']} ({pr['how']})" if pr else "stated no PR" if none else "no PR found") for key, (pr, none) in found.items()]


def worktree_of(key: str, agent: dict, s: dict, guess: bool = False) -> str:
    """The worktree a run works in: the one `nzfleet start` made for its task or, with `guess`, the only one made while it ran."""
    base = runs.base_key(key)
    for known in s["worktrees"].values():
        if known.get("agent") == base:
            return known["path"]
    if not guess:
        return ""
    made = [path for path in world.worktrees_made_between(agent["started"], agent.get("finished", time.time()))
            if os.path.dirname(path) == WORK]
    return made[0] if len(made) == 1 else ""


def base_of(path: str, s: dict) -> str:
    """The commit a worktree was started from, or main's head when it was not made by `nzfleet start`."""
    return next((w["base"] for w in s["worktrees"].values() if w["path"] == path), world.MAIN)


def where_it_stopped(path: str, s: dict):
    """What a worktree holds above its base, as runs.Stopped, or None when it is gone."""
    if not path or not os.path.isdir(path):
        return None
    base = base_of(path, s)
    commits = world.git("log", "--oneline", f"{base}..HEAD", cwd=path, check=False).splitlines()
    changes = world.git("status", "--porcelain", cwd=path, check=False).splitlines()
    return runs.Stopped(path, tuple(commits), tuple(changes))


def mark_no_change(key: str) -> None:
    """Records that a task which stated `PR: none` left nothing in its worktree, so the chain behind it need not wait."""
    with state.change() as s:
        found = runs.latest(key, s["agents"])
        if found:
            found[1]["no_change"] = True


def mark_stopped(key: str, why: str, nudge: bool, now: float) -> None:
    """Records a run that was stopped on purpose as unfinished, so it is resumed like one that died."""
    with state.change() as s:
        agent = s["agents"][key]
        agent.update(status="unfinished", finished=now, nudge=nudge, summary=(agent.get("summary", "") + "\n" + why).strip())


def stop(key: str, now: float) -> str:
    """Ends the task's running run on a person's say-so and records it `stopped`, which nothing resumes. Returns why not, or ''."""
    with state.change() as s:
        found = runs.latest(runs.base_key(key), s["agents"])
        if found is None:
            return f"no run of {key} has started"
        run_id, agent = found
        if agent["status"] != "running":
            return f"{run_id} is not running ({agent['status']})"
        agent.update(status="stopped", finished=now)
        pid, unit = agent.get("pid"), agent.get("unit")
    if unit:
        machine.stop_unit(unit)
    elif pid and _alive(pid):
        procs.kill_group(pid)
    return ""


def resume(dry_run: bool = False, now: float | None = None, barred: set | None = None) -> list:
    """Starts each unfinished run that is not held once more, in the worktree it left, told where it stopped. Returns what it started."""
    if config.AGENT_SLOTS <= 0:
        return []
    now = time.time() if now is None else now
    s = state.read()
    agents = s.get("agents", {})
    in_flight = task_queue.in_flight_members(s)
    out = []
    for key, a in agents.items():
        if rebases.in_open_train(key, in_flight) or rebases.targets_barred(key, barred or set()):
            continue
        if a.get("status") == "unfinished" and runs.attempt(key) > runs.RESUME_LIMIT and not _alive(a.get("pid") or 0):
            abandon(key, "no resumptions left")
            continue
        if not runs.needs_resume(a, agents, key, now) or runs.base_key(key) in s["holds"]:
            continue
        first = runs.lineage(runs.base_key(key), agents)[0][1]
        path = a.get("worktree") or worktree_of(key, a, s, guess=True)
        profile = s.get("agent_session", {})
        kept = a.get("provider_selection", a.get("provider", "configured"))
        provider = kept if a.get("pinned") else profile.get("provider", kept)
        command = {"codex": AGENT_CODEX, "claude": AGENT_CLAUDE}.get(provider, COMMAND)
        session = a.get("session", "") if dispatch.has_sessions(command) else ""
        task = runs.resume_task("" if session else first["task"], where_it_stopped(path, s), bool(a.get("nudge")))
        next_key = runs.run_key(runs.base_key(key), runs.attempt(key) + 1)
        out.append(start(next_key, task, a["model"], dry_run, a.get("area", ""), path,
                         pr=a.get("for_pr"), resume_session=session,
                         provider_override=kept, pinned=bool(a.get("pinned"))))
        if not dry_run and (refused := state.read()["refused"].get(next_key)):
            abandon(key, refused["reason"])
    return out


def abandon(key: str, why: str) -> None:
    """Ends an unfinished run's chance of resuming, so it is not retried every tick; a person decides what happens to the task."""
    with state.change() as s:
        agent = s["agents"][key]
        if agent["status"] == "unfinished":
            agent.update(status=runs.ABANDONED, summary=(agent.get("summary", "") + f"\nnot resumed: {why}").strip())


def _process_start(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _alive(pid: int, process_start: str | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and (process_start is None or fields[19] == process_start)
    except (OSError, IndexError):
        return False
