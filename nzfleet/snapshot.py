"""One picture of the fleet, built from what was observed and nothing else. The daemon writes it every tick; `status` reads it."""

from __future__ import annotations

import dataclasses

from . import attention, choice, config, costs, holds, owners, plan, progress, redact, runs, selfupdate, train, trainpr

VERSION = 1
SECTIONS = ("headline", "needs_you", "attention", "pipeline", "agents", "machine", "cost", "held", "flakes")
ALIASES = {"needs": ("needs_you", "attention"), "progress": ("agents", "pipeline")}
SHOWN_BATCHES = 8


def _standing(s: dict) -> dict:
    """Where each PR an agent produced stands, from what earlier ticks recorded."""
    numbers = {a["pr"]["number"] for a in s["agents"].values() if a.get("pr")} | holds.pending_prs(s["todo"])
    return {n: "merged" if str(n) in s["merged_at"] else "passed" if str(n) in s["passed"] else "waiting" for n in numbers}


def _since(then: float | None, now: float) -> float | None:
    return None if then is None else max(0.0, now - then)


def _main(s: dict, now: float) -> dict:
    ci = s.get("main_ci") or {}
    current = s.get("main_ci_current") or {}
    last = max(s["deploys"].items(), key=lambda item: item[1]["at"], default=None)
    outcome = current.get("conclusion", "unknown")
    green = True if outcome == "success" else False if outcome == "failure" else None
    first_observed = current.get("first_observed_at", current.get("at"))
    return {"sha": current.get("sha", ""), "green": green, "for": _since(first_observed, now),
            "status": current.get("status", "unknown"), "failed_jobs": current.get("failed_jobs", {}), "conclusion": outcome, "run_id": current.get("run_id"),
            "last_completed_sha": ci.get("sha", ""), "last_completed_green": ci.get("ok"),
            "last_completed_for": _since(ci.get("completed_at"), now),
            "deployed": last[1]["what"] if last else "", "deployed_sha": last[0] if last else "",
            "deploy_age": _since(last[1]["at"], now) if last else None}


def _agents(w: dict, cores_of: dict) -> tuple:
    s, now = w["state"], w["now"]
    rows, spend = [], {}
    for r in w["cost_records"]:
        usd, turns = spend.get(r["key"], (0.0, 0))
        spend[r["key"]] = (usd + r["cost"], turns + 1)
    context = {}
    for r in sorted(w["cost_records"], key=lambda r: r["at"]):
        context[r["key"]] = r.get("ctx", 0)
    for base in dict.fromkeys(runs.base_key(k) for k in s["agents"]):
        found = runs.latest(base, s["agents"])
        if found is None:
            continue
        key, agent = found
        if agent["status"] != "running" and now - agent.get("finished", now) > costs.DAY:
            continue
        seen = s["samples"].get(key)
        sample = progress.Sample(**seen) if seen else progress.Sample(0.0, now, now, now)
        row = progress.agent_row(key, "stuck" if runs.run_state(base, s["agents"]) == "stuck" else agent["status"],
                                 w["activity"].get(key) or progress.Activity(0, 0, None, None, None), sample, agent["started"],
                                 s["notes"].get(base, []), now)
        usd, turns = spend.get(base, (0.0, 0))
        rows.append({**dataclasses.asdict(row), "provider": agent.get("provider", "configured"), "model": agent["model"], "area": agent.get("area", ""), "age": now - agent["started"],
                     "cores": cores_of.get(key, 0.0), "turns": turns, "context": context.get(base, 0), "usd": round(usd, 2),
                     "pr": (agent.get("pr") or {}).get("number")})
    return rows, spend


def _pipeline(w: dict) -> dict:
    s, now = w["state"], w["now"]
    recent = sorted(s["batches"].items(), key=lambda kv: kv[1]["started"])[-SHOWN_BATCHES:]
    chains = progress.chain_rows(s["todo"], s["agents"], s["batches"], s["passed"], s["merged_at"], _standing(s), now)
    return {"queue": plan.land_command(train.open_for(s["trains"], s["notified"])),
            "batches": [{"id": bid, "group": b.get("group", ""), "status": b["status"], "age": (b.get("finished") or now) - b["started"],
                         "members": [n for n, _ in b["members"]], "line": b.get("line", "")} for bid, b in recent],
            "trains": [{"id": t["id"], "status": t["status"], "members": [n for n, _ in t["members"]]}
                       for t in s["trains"].values() if t["status"] == train.OPEN],
            "train_refused": s["train_refused"].get("why", ""),
            "train_prs": [trainpr.status_row(pr, record, s["batches"]) for pr, record in sorted(s["train_prs"].items())],
            "main": _main(s, now), "chains": [dataclasses.asdict(c) for c in chains],
            "waiting": [f"{t['key']} ({choice.label(t)})" if t.get("provider") else t["key"]
                        for t in s["todo"] if t["key"] not in s["agents"] and t["key"] not in s["holds"]]}


def _machine(w: dict, owner_of: dict) -> tuple:
    previous = w["previous"].get("machine", {})
    elapsed = w["now"] - w["previous"].get("at", w["now"])
    used, by_agent, seconds = owners.rates(w["procs"], owner_of, previous.get("_cpu", {}), elapsed)
    cpu = {o: {"cores": used[o], "quota": w["quotas"].get(o)} for o in owners.OWNERS}
    return {**w["machine"], "cpu_by_owner": cpu, "_cpu": seconds}, by_agent


def _held(s: dict, now: float) -> dict:
    return {"holds": [{"key": k, "why": h["why"], "age": now - h["since"], "stale": now - h["since"] >= attention.STALE_HOLD}
                      for k, h in sorted(s["holds"].items(), key=lambda kv: kv[1]["since"])],
            "refused": [{"key": k, "reason": r["reason"]} for k, r in sorted(s["refused"].items())]}


def _headline(w: dict, cost: dict, you: list, attn: list, pipeline: dict) -> dict:
    main = pipeline["main"]
    load = w["machine"]["load"][0]
    return {"main_green": main["green"], "main_for": main["for"], "main_status": main["status"],
            "main_last_completed_green": main["last_completed_green"], "main_last_completed_for": main["last_completed_for"],
            "deploy_age": main["deploy_age"],
            "ready": len(w["state"]["notified"]), "load": load, "cores": w["machine"]["cores"], "usd_hour": cost["hour"], "codex_hour": cost["codex_hour"],
            "needs_you": len(you), "attention": len(attn)}


def snapshot(w: dict) -> dict:
    """The whole picture as plain data. `w` holds everything observed: state, processes, activity, machine, cost records, the last snapshot."""
    s, now = w["state"], w["now"]
    owner_of = owners.classify(w["procs"], w["agent_pids"], w["repo"])
    machine, cores_of = _machine(w, owner_of)
    agent_rows, spend = _agents(w, cores_of)
    pr_of_key = {runs.base_key(k): a["pr"]["number"] for k, a in s["agents"].items() if a.get("pr")}
    cost = {**costs.summary(w["cost_records"], now, w["day_start"], pr_of_key), **costs.codex_runs(s["agents"], now, w["day_start"])}
    you = attention.needs_you(s)
    attn = attention.needs_attention(s, w["procs"], owner_of, w["activity"], spend, now)
    if config.CONFIG_PROBLEM:
        attn.insert(0, {"kind": "config", "subject": "CPU limit", "text": config.CONFIG_PROBLEM,
                        "fix": "correct NZFLEET_MAX_CPU_PERCENT in the nzfleet environment file"})
    behind = selfupdate.attention(w.get("behind", ()), now)
    attn = attn + ([{"kind": "code", "subject": "nzfleet", "text": behind, "fix": "nzfleet version"}] if behind else [])
    issue = w.get("postsubmit_issue")
    if issue is not None:
        number = issue["number"]
        attn.append({"kind": "postsubmit", "subject": f"issue #{number}",
                     "text": "postsubmit repeats are red; repair trains retain their exact-tree gates",
                     "fix": f"review issue #{number}"})
    pipeline = _pipeline(w)
    return redact.tree({"version": VERSION, "at": now, "blocked": s["blocked"], "headline": _headline(w, cost, you, attn, pipeline), "needs_you": you, "attention": attn[:5],
            "pipeline": pipeline, "agents": agent_rows, "machine": machine, "cost": cost, "held": _held(s, now),
            "flakes": [{"test": t, "count": c, "last_seen": ls} for t, c, ls in plan.flake_rows(s["flakes"])]},
                       tuple(w.get("secret_paths", ())))


def section(snap: dict, names: list) -> dict:
    """The named sections (or aliases) of a snapshot, in snapshot order."""
    wanted = [n for name in names for n in ALIASES.get(name, (name,))]
    return {k: snap[k] for k in SECTIONS if k in wanted}
