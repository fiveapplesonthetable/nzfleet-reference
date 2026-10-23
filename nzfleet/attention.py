"""What needs the owner and what needs an agent, each as a line with the command that fixes it. Pure functions of the state."""

from __future__ import annotations

from . import config, owner, plan, runs, supervise, train

STUCK_AFTER = 20 * 60
STALE_HOLD = 24 * 3600
COST_LIMIT = 15.0   # dollars one task may spend before it is flagged
TURN_LIMIT = 150    # assistant messages one task may use before it is flagged


def _line(kind: str, subject: str, text: str, fix: str = "") -> dict:
    return {"kind": kind, "subject": subject, "text": text, "fix": fix}


def needs_you(s: dict) -> list:
    """The merge command for the verified train, decisions waiting, and PRs that failed outside their code."""
    out = []
    command = plan.land_command(train.open_for(s["trains"], s["notified"]))
    if command:
        out.append(_line("merge", command, f"{len(s['notified'])} verified: {' '.join('#%s' % n for n in s['notified'])}", command))
    for key, sentence in sorted(s["needs_person"].items()):
        out.append(_line("decision", key, sentence.splitlines()[0], f"answer on issue #{config.OWNER_ISSUE}"))
    for n, u in sorted(s["untied"].items(), key=lambda kv: int(kv[0])):
        if owner.stopped(u, u["sha"]):
            out.append(_line("untied", f"#{n}", f"#{n} failed outside its code {u['times']} times, not retried: {u['why']}",
                             "fix the machine, then nzfleet tick"))
    return out


def _stuck(s: dict, activity: dict, now: float) -> list:
    out = []
    for key, agent in s["agents"].items():
        if agent["status"] != "running":
            continue
        act = activity.get(key)
        sample = s["samples"].get(key)
        last = max(t for t in (act and act.last_commit, act and act.last_change, agent["started"]) if t is not None)
        parked = sample is not None and now - sample["busy_at"] < STUCK_AFTER
        if now - last >= STUCK_AFTER and not parked:
            out.append(_line("stuck", key, f"{key} wrote nothing for {int((now - last) // 60)} minutes and its CPU is idle",
                             f"nzfleet stop {runs.base_key(key)}"))
    return out


def _burners(procs: list, owner_of: dict) -> list:
    groups: dict = {}
    for p in procs:
        if owner_of.get(p.pid, ("",))[0] == "burners":
            groups.setdefault((p.name, p.cwd), []).append(p)
    out = []
    for (name, cwd), found in sorted(groups.items()):
        oldest = int(max(p.age for p in found) // 60)
        pids = " ".join(str(p.pid) for p in found[:8])
        out.append(_line("burner", f"{name} in {cwd}", f"{len(found)} {name} burner(s) in {cwd}, the oldest {oldest} minutes; nzfleet stops them at 10",
                         f"kill -TERM {pids}" + (" ..." if len(found) > 8 else "")))
    return out


def _duplicates(s: dict, alive: set) -> list:
    return [_line("duplicate batch", bid, f"batch {bid} repeats an older one over the same PRs", f"kill -TERM -{pid}")
            for bid, pid in supervise.duplicate_batches(s["batches"], alive)]


def _retried(s: dict) -> list:
    out = []
    for base in dict.fromkeys(runs.base_key(k) for k in s["agents"]):
        found = runs.latest(base, s["agents"])
        if found and runs.attempt(found[0]) > 2 and found[1]["status"] not in ("done", "retired"):
            out.append(_line("retried", base, f"{base} is on run {runs.attempt(found[0])} ({found[1]['status']})",
                             f"nzfleet hold {base} --why \"...\""))
    return out


def _over_budget(s: dict, spend: dict) -> list:
    out = []
    running = {runs.base_key(k) for k, a in s["agents"].items() if a["status"] == "running"}
    for key in sorted(running):
        usd, turns = spend.get(key, (0.0, 0))
        if usd > COST_LIMIT or turns > TURN_LIMIT:
            out.append(_line("budget", key, f"{key} has used ${usd:.0f} over {turns} turns (limits ${COST_LIMIT:.0f}, {TURN_LIMIT})", f"nzfleet stop {key}"))
    return out


def _stale_holds(s: dict, now: float) -> list:
    return [_line("stale hold", key, f"{key} held for {int((now - h['since']) // 3600)} h: {h['why']}", f"nzfleet release {key}")
            for key, h in sorted(s["holds"].items(), key=lambda kv: kv[1]["since"]) if now - h["since"] >= STALE_HOLD]


def _merged_members(s: dict) -> list:
    merged = set(s["merged_at"])
    out = [_line("merged in batch", bid, f"batch {bid} includes #{n}, already merged", f"kill -TERM -{b['pid']}")
           for bid, b in sorted(s["batches"].items()) if b["status"] == "running"
           for n, _ in b["members"] if str(n) in merged]
    out += [_line("merged in train", t["id"], f"train {t['id']} includes #{n}, already merged", "nzfleet tick")
            for t in s["trains"].values() if t["status"] == train.OPEN for n, _ in t["members"] if str(n) in merged]
    return out


def _failed(s: dict) -> list:
    return [_line("failed", f"#{n}", f"#{n} failed at {f['sha'][:8]}: {f['why']}", "nzfleet next") for n, f in sorted(s["failed"].items(), key=lambda kv: int(kv[0]))] + \
           [_line("hands", f"#{n}", f"#{n} does not replay cleanly", "nzfleet next") for n, _ in sorted(s["hands"].items(), key=lambda kv: int(kv[0]))]


def needs_attention(s: dict, procs: list, owner_of: dict, activity: dict, spend: dict, now: float) -> list:
    """Everything an agent should act on, with the command that does it."""
    alive = {p.pid for p in procs}
    return (_burners(procs, owner_of) + _stuck(s, activity, now) + _duplicates(s, alive) + _merged_members(s)
            + _over_budget(s, spend) + _retried(s) + _stale_holds(s, now) + _failed(s))
