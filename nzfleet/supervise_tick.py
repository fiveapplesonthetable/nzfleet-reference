"""The supervisor's hands: samples agents every tick, and once an hour looks after the machine and says how things are going.

Decisions are in supervise.py and progress.py; this reads the host through `Host` and carries the decisions out."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import asdict, dataclass
from typing import Callable

from . import agents, config, jev, jev_points, machine, observe, owner_tick, progress, procs, runs, state, supervise, train, world

INTERVAL = 3600
DAY = 24 * 3600
NOTICE = ("nzfleet stopped {count} CPU burner process(es) you left running for over 10 minutes. A starvation test runs "
          "under `nzfleet starve -- <command>`, which gives it a CPU slice of its own and cleans up after itself.\n")


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


@dataclass
class Host:
    """What the supervisor asks of the machine; the defaults are the real one."""
    now: Callable = time.time
    processes: Callable = observe.processes
    activity: Callable = observe.activity
    alive: Callable = procs.alive
    kill: Callable = _terminate
    kill_group: Callable = procs.kill_group
    busy: Callable = machine.busy
    load: Callable = machine.load
    cpu_percent: Callable = machine.cpu_percent
    free_gb: Callable = machine.free_gb
    comment: Callable = world.comment
    waits: Callable = observe.waits
    shadow: Callable = jev_points.shadow_jev


def due(last: float, now: float) -> bool:
    return now - last >= INTERVAL


def _stop(host: Host, table: list, pid: int) -> bool:
    """Stops an agent's process group, only if the process is alive and leads its own group, as the ones nzfleet started do."""
    found = next((p for p in table if p.pid == pid), None)
    if found is None or found.pgid != pid:
        return False
    host.kill_group(pid)
    return True


def _worktree(s: dict, key: str, agent: dict) -> str:
    return agent.get("worktree") or agents.worktree_of(key, agent, s)


def _stalled_sentence(key: str, worktree: str, notes: list) -> str:
    last = notes[-1] if notes else None
    said = f"Its last note: {last['phase']}: {last['line']}" if last else "It has left no note."
    return (f"{key} was nudged and has again written nothing for over 45 minutes while its CPU is idle.\n"
            f"Worktree: {worktree or 'unknown'}. {said}\nIt needs: someone to look at what it is waiting for, and to stop it with "
            f"`nzfleet kill` or answer what blocks it.")


def _shadow_step(host: Host, chooser, s: dict, key: str, agent: dict, act, sample, now: float, nudged: bool,
                 table: list, pending: dict, needs: dict) -> str | None:
    """Logs what Jev would do with this agent beside the rule's step, and judges the previous such choice once its time has come."""
    if chooser is None:
        return None
    base, log = runs.base_key(key), jev.Log()
    slot, notes = f"supervise:{base}", s["notes"].get(base, [])
    held = pending.get(slot)
    if held:
        progressed = jev_points.progressed(act, held["at"], held["commits"])
        elapsed = now - held["at"]
        result = jev_points.outcome(held["acted"], progressed, elapsed)
        try:
            record = next((row for row in log.read() if row["id"] == held["id"]), {})
            jev_result = jev_points.outcome(record.get("jev") or "stop", progressed, elapsed)
            if result and log.set_outcome(held["id"], result, jev_result):
                pending.pop(slot)
        except OSError:
            pass
    state_now = jev_points.supervise_state(act, sample, agent["started"], now, nudged, runs.attempt(key), progress.current_phase(notes, now),
                                           notes[-1]["at"] if notes else None, host.waits(table, agent["pid"]),
                                           any(b["status"] == "running" for b in s["batches"].values()))
    rule = jev_points.supervise_rule(state_now, ())
    selective = frozenset()
    if chooser is not None and config.JEV_ACT:
        eligible = frozenset({jev_points.Supervision.WAIT.value, jev_points.Supervision.NUDGE.value})
        if jev.agreement_allows(log.read(), jev_points.SUPERVISE.name, eligible):
            selective = eligible
    decision = jev_points.consult(jev_points.SUPERVISE, rule, state_now, jev_points.SUPERVISE_OPTIONS,
                                 jev_points.SUPERVISE_INSTRUCTIONS, chooser, now, log, selective)
    if decision and slot not in pending:
        record = next((row for row in reversed(log.read()) if row["id"] == decision), None)
        if record and record.get("escalation"):
            needs[f"jev:{base}"] = f"Jev decision {decision} at {jev_points.SUPERVISE.name} needs attention: {record['escalation']}"
            return jev.STOP
        needs.pop(f"jev:{base}", None)
        acted = record["jev"] if record and record["acted"] == "jev" else rule
        if record and record.get("acted"):
            pending[slot] = {"id": decision, "acted": acted, "at": now, "commits": act.commits}
        return acted if acted != rule else None
    return None


def watch_agents(host: Host, tell: Callable) -> None:
    """Every tick: samples each running agent's process tree and worktree, resumes a hung one, nudges a stalled one once."""
    s = state.read()
    now, table = host.now(), host.processes()
    samples, needs, worktrees, pending = dict(s["samples"]), dict(s["needs_person"]), {}, dict(s.get("jev_pending", {}))
    chooser = host.shadow()
    for key, agent in s["agents"].items():
        if agent["status"] != "running" or not host.alive(agent["pid"]):
            continue
        path = _worktree(s, key, agent)
        worktrees[key] = path
        prev = progress.Sample(**samples[key]) if key in samples else None
        sample = progress.take_sample(prev, supervise.tree_cpu(table, agent["pid"]), now)
        samples[key] = asdict(sample)
        act = host.activity(path, agents.base_of(path, s))
        earlier = runs.lineage(runs.base_key(key), s["agents"])[:-1]
        nudged = any(a.get("nudge") for _, a in earlier)
        action = supervise.step(progress.verdict(act, sample, agent["started"], now), nudged)
        selected = _shadow_step(host, chooser, s, key, agent, act, sample, now, nudged, table, pending, needs)
        if selected == jev_points.Supervision.WAIT.value:
            action = supervise.Step.NONE
        elif selected == jev_points.Supervision.NUDGE.value:
            action = supervise.Step.NUDGE
        elif selected == jev.STOP:
            action = supervise.Step.NONE
        stall = f"stalled-{key}"
        needs.pop(stall, None)
        if action is supervise.Step.EMAIL:
            needs[stall] = _stalled_sentence(key, path, s["notes"].get(runs.base_key(key), []))
        elif action is not supervise.Step.NONE and _stop(host, table, agent["pid"]):
            nudge = action is supervise.Step.NUDGE
            agents.mark_stopped(key, "stopped: nothing written for 45 minutes with the CPU idle" if nudge
                                else "stopped: no file change and no CPU for 30 minutes", nudge, now)
            tell(f"{key} " + ("stalled for 45 minutes; nudged and resuming in its worktree" if nudge
                              else "hung for 30 minutes; stopped, to be resumed in its worktree"))
    with state.change() as st:
        st["samples"], st["needs_person"], st["jev_pending"] = samples, needs, pending
        for key, path in worktrees.items():
            if path and key in st["agents"] and not st["agents"][key].get("worktree"):
                st["agents"][key]["worktree"] = path


def rows(s: dict, host: Host, standing: dict) -> tuple:
    """Agent rows for tasks running, unfinished or finished within a day, and one row per chain."""
    now, agent_rows = host.now(), []
    for base in dict.fromkeys(runs.base_key(k) for k in s["agents"]):
        found = runs.latest(base, s["agents"])
        if found is None:  # a run series with a gap at its start has no latest run to show
            continue
        key, agent = found
        if agent["status"] != "running" and now - agent.get("finished", now) > DAY:
            continue
        path = _worktree(s, key, agent)
        seen = s["samples"].get(key)
        sample = progress.Sample(**seen) if seen else progress.Sample(0.0, now, now, now)
        cpu = sum(s["samples"].get(k, {}).get("cpu", 0.0) for k, _ in runs.lineage(base, s["agents"]))
        state_name = "stuck" if runs.run_state(base, s["agents"]) == "stuck" else agent["status"]
        agent_rows.append(progress.agent_row(key, state_name, host.activity(path, agents.base_of(path, s)),
                                             progress.Sample(cpu, sample.at, sample.alive_at, sample.busy_at),
                                             agent["started"], s["notes"].get(base, []), now))
    chains = progress.chain_rows(s["todo"], s["agents"], s["batches"], s["passed"], s["merged_at"], standing, now)
    return agent_rows, chains


def progress_text(host: Host, standing: dict) -> str:
    return progress.table(*rows(state.read(), host, standing))


def _notify_worktree(s: dict, cwd: str, count: int) -> None:
    for path in {w["path"] for w in s["worktrees"].values()} | {a.get("worktree", "") for a in s["agents"].values()}:
        if path and (cwd == path or cwd.startswith(path.rstrip("/") + "/")):
            os.makedirs(os.path.join(path, ".nzrun"), exist_ok=True)
            with open(os.path.join(path, ".nzrun", "NZFLEET-NOTICE.txt"), "a") as f:
                f.write(NOTICE.format(count=count))


def stop_burners(host: Host, tell: Callable) -> None:
    """Every tick: stops a burner past the time box wherever it runs, says which agent owned it, and comments on that task's PR."""
    s, table = state.read(), host.processes()
    roots = {a["pid"] for a in s["agents"].values() if a["status"] == "running"}
    for p in supervise.burners(table, roots, config.WORK):
        host.kill(p.pid)
        _notify_worktree(s, p.cwd, 1)
        key = supervise.owner(p, table, s["agents"])
        who = f"agent {key}" if key else "no agent nzfleet knows"
        tell(f"stopped a {p.name} burner (pid {p.pid}, {p.cgroup or 'no cgroup'}) owned by {who} that ran {int(p.age // 60)} minutes in {p.cwd}; "
             "`nzfleet starve` is the way to starve a test")
        pr = runs.recorded_pr(runs.base_key(key), s["agents"]) if key else None
        if pr:
            try:
                host.comment(pr["number"], f"nzfleet stopped a `{p.name}` burner that {key} left running for {int(p.age // 60)} minutes outside "
                             "`nzfleet starve`. A starvation test runs under `nzfleet starve -- <command>`.")
            except world.Failed as e:
                tell(f"could not comment on #{pr['number']} about the burner: {e}")


def _stop_duplicate_batches(host: Host, s: dict, table: list, tell: Callable) -> None:
    alive = {p.pid for p in table}
    for bid, pid in supervise.duplicate_batches(s["batches"], alive):
        with state.change() as st:
            st["batches"][bid]["status"] = "superseded"  # not "running", so the tick does not count it as a failure of its members
        if _stop(host, table, pid):
            tell(f"batch {bid} stopped: an older batch checks the same PRs")


def _unblock_chains(host: Host, s: dict, standing: dict, tell: Callable) -> list:
    """Says why each idle chain is not starting; a `PR: none` that left nothing in its worktree is not a reason, so it is lifted."""
    running = sum(1 for a in s["agents"].values() if a["status"] == "running")
    lines = []
    for idle in supervise.idle_chains(s["todo"], s["agents"], standing, s["holds"], running, config.AGENT_SLOTS, host.busy(), s.get("area_heads", {})):
        found = runs.latest(idle.no_pr_of, s["agents"]) if idle.no_pr_of else None
        path = _worktree(s, *found) if found else ""
        act = host.activity(path, agents.base_of(path, s)) if path else None
        if act is not None and act.commits == 0 and act.files == 0:
            agents.mark_no_change(idle.no_pr_of)
            tell(f"chain {idle.area} was held by {idle.no_pr_of} stating `PR: none`; its worktree holds nothing, so the chain goes on")
        else:
            lines.append(f"chain {idle.area} is idle at {idle.key}: {idle.why}")
    return lines


def _mail_green(tell: Callable) -> None:
    """A set that passed and was never emailed (the tick's own mail may have failed before it got that far) goes out now."""
    s = state.read()
    order = [n for n in s["notified"] if str(n) in s["passed"]]
    if order:
        owner_tick.ready_set(order, {n: s["passed"][str(n)]["sha"] for n in order}, train.open_for(s["trains"], order), tell)


def _guard(name: str, step: Callable, lines: list) -> None:
    try:
        step()
    except Exception as e:  # noqa: BLE001 - one failing check must not stop the others
        lines.append(f"supervision: {name} failed: {type(e).__name__}: {e}")


def hourly(host: Host, standing: Callable, tell: Callable, event: Callable) -> list:
    """The hourly pass. Returns the lines it could not act on; the health line goes to `event` and a need for a person to state."""
    lines, table = [], host.processes()
    s = state.read()
    _guard("duplicate batches", lambda: _stop_duplicate_batches(host, s, table, tell), lines)
    _guard("idle chains", lambda: lines.extend(_unblock_chains(host, state.read(), standing(state.read()), tell)), lines)
    _guard("green batches", lambda: _mail_green(tell), lines)
    _guard("health", lambda: lines.append(_health(host, standing)), lines)
    for line in lines:
        event(line)
    with state.change() as st:
        st["supervised"] = host.now()
    return lines


def _health(host: Host, standing: Callable) -> str:
    s = state.read()
    agent_rows, chains = rows(s, host, standing(s))
    running = sum(1 for a in s["agents"].values() if a["status"] == "running")
    stuck = len(runs.stuck_runs(s["agents"], host.now()))
    used = host.cpu_percent()
    cpu = f"load {host.load():.0f}" if used is None else f"cpu {used:.0f}% of {config.MAX_CPU_PERCENT:.0f}%"
    return (f"health: {cpu}, {host.free_gb()} GB free, {running} agents running, {stuck} stuck; "
            + progress.health_line(agent_rows, chains))
