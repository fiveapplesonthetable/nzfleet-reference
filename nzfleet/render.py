"""A snapshot as text for a person. Every section is a pure function of its own part of the snapshot; nothing here reads the machine."""

from __future__ import annotations

from . import blocked, progress, rootdisk, snapshot as snap


def _span(seconds) -> str:
    if seconds is None:
        return "-"
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h" if minutes < 2880 else f"{minutes // 1440} d"


def headline(h: dict) -> str:
    main = f"main {h.get('main_status', 'unknown')}"
    if "main_status" in h and h["main_green"] is not None:
        main += f" ({'green' if h['main_green'] else 'RED'})"
    if h.get("main_for") is not None:
        main += f" for {_span(h['main_for'])}"
    completed_green = h.get("main_last_completed_green", h.get("main_green"))
    completed_for = h.get("main_last_completed_for", h.get("main_for"))
    completed_age = f"{_span(completed_for)} ago" if completed_for is not None else "age unknown"
    main += f"; last completed {'green' if completed_green else 'RED' if completed_green is False else 'unknown'} {completed_age}"
    return (f"{main}; last deploy record {_span(h['deploy_age'])} ago; {h['ready']} ready to merge; load {h['load']:.0f} of {h['cores']} cores; "
            f"${h['usd_hour']:.2f} in the last hour (Claude); {h.get('codex_hour', 0)} Codex runs; {h['needs_you']} need you, {h['attention']} need attention")


def lines(title: str, rows: list, empty: str) -> list:
    return [title] + ([f"  {r['text']}" + (f"\n    -> {r['fix']}" if r["fix"] else "") for r in rows] or [f"  {empty}"])


def pipeline(p: dict) -> list:
    out = ["PIPELINE", f"  merge queue: {p['queue'] or 'nothing ready'}"]
    main = p["main"]
    status = {"in_progress": "in progress", "requested": "requested", "waiting": "waiting"}.get(main.get("status"), main.get("status", "unknown"))
    current = f"  main {main.get('sha', '')[:8] or '-'} CI: {status}"
    if main.get("run_id") is not None:
        current += f" run #{main['run_id']}"
    if main["green"] is not None:
        current += f" ({'green' if main['green'] else 'RED'})"
    completed_sha = main.get("last_completed_sha", main.get("sha", ""))
    completed_green = main.get("last_completed_green", main.get("green"))
    completed_for = main.get("last_completed_for", main.get("for"))
    completed_age = f"{_span(completed_for)} ago" if completed_for is not None else "age unknown"
    completed = f"; last completed {completed_sha[:8] or '-'} " \
                f"{'green' if completed_green else 'RED' if completed_green is False else 'unknown'} {completed_age}"
    deployed = f"; last deploy record for {main.get('deployed_sha', '')[:8]}: {main['deployed']}" if main.get("deployed") else ""
    out.append(current + completed + deployed)
    out += [f"  main job failed: {job}: {line}" for job, line in sorted(main.get("failed_jobs", {}).items())]
    out += [f"  {b['id']:24} {b['status']:15} {_span(b['age']):>7}  {b['members']}  {b['line']}" for b in p["batches"]]
    out += [f"  train #{t['pr']}: in {t['members']}, left out {t['left_out']}, dropped {t['dropped']}; "
            f"compile {'ok' if t['compiled'] else 'failed'}; batch {t['verdict']}; owner notice {t.get('notice', 'review pending')}"
            for t in p.get("train_prs", [])]
    out += [f"  last train refused: {p['train_refused']}"] if p.get("train_refused") else []
    out += [f"  chain {c['area']}: head {c['head'] or 'none, all merged'}, {c['steps_hour']} steps in the last hour" for c in p["chains"]]
    out.append(f"  work queue: {len(p['waiting'])} waiting: {', '.join(p['waiting'][:6])}")
    return out


def agents(rows: list) -> list:
    out = ["AGENTS"]
    for a in rows:
        usd = "codex" if a.get("provider") == "codex" else f"${a['usd']:.2f}"
        phase = f"{a['phase']} {_span(a['phase_for'])}: {a['note']}" if a["phase"] else "no note yet"
        out.append(f"  {a['key']:26}{a['state']:9}{a.get('provider', 'configured')}/{a['model']:20}{_span(a['age']):>8} commit {_span(a['since_commit']):>7} change "
                   f"{_span(a['since_change']):>7} {a['cores']:>4.1f} cpu {a['turns']:>4} turns {usd:>7}  {phase}")
    return out + ([] if rows else ["  none"])


def machine(m: dict) -> list:
    out = ["MACHINE", f"  load {m['load'][0]:.0f} {m['load'][1]:.0f} {m['load'][2]:.0f} of {m['cores']} cores; memory "
           f"{m['mem_free_gb']} of {m['mem_total_gb']} GB free" + (f"; {m['temp_c']:.0f} C" if m.get("temp_c") is not None else "")]
    out += [f"  disk {d['mount']}: {d['free_gb']} GB free, {d['used_percent']}% used"
            + (" (FILLING)" if d["used_percent"] >= rootdisk.WARN_PERCENT else "") for d in m["disk"]]
    out.append("  CPU by owner (cores in use / quota):")
    out += [f"    {o:14}{v['cores']:>6.1f} / {v['quota'] if v['quota'] is not None else '-'}" for o, v in m["cpu_by_owner"].items()]
    return out


def cost(c: dict) -> list:
    out = ["COST",
           f"  claude: last hour ${c['hour']:.2f}, today ${c['today']:.2f}, 7 days ${c['week']:.2f}",
           f"  codex runs: {c.get('codex_hour', 0)} in the last hour, {c.get('codex_today', 0)} today"]
    out.append("  by place: " + ", ".join(f"{k} ${v:.0f}" for k, v in c["by_place"].items()))
    out.append("  by model: " + ", ".join(f"{k} ${v:.0f}" for k, v in c["by_model"].items()))
    out.append("  top today: " + (", ".join(f"{t['key']} ${t['usd']:.2f}" for t in c["top_today"]) or "none"))
    out.append("  per merged PR: " + (", ".join(f"#{n} ${v:.2f}" for n, v in c["per_pr"].items()) or "none"))
    return out


def held(h: dict, expanded: bool) -> list:
    stale = [x for x in h["holds"] if x["stale"]]
    if not expanded:
        return [f"HELD AND STALE  {len(h['holds'])} held ({len(stale)} over 24 h), {len(h['refused'])} refused; nzfleet status --section held"]
    return (["HELD AND STALE"] + [f"  {x['key']:28} {_span(x['age'])}: {x['why']}" for x in h["holds"]]
            + [f"  {x['key']} not dispatched: {x['reason']}" for x in h["refused"]])


def flakes(rows: list) -> list:
    return [f"  {r['count']:3} {_span(r['last_seen_ago']):>8} ago  {r['test']}" for r in rows] or ["no flakes recorded"]


def progress_table(shot: dict) -> str:
    fields = {f for f in progress.AgentRow.__dataclass_fields__}
    rows = [progress.AgentRow(**{k: v for k, v in a.items() if k in fields}) for a in shot["agents"]]
    chains = [progress.ChainRow(**c) for c in shot["pipeline"]["chains"]]
    return progress.table(rows, chains)


def text(shot: dict, names: list | None = None) -> str:
    """The named sections in order, or everything with the held section collapsed."""
    wanted = set(snap.section(shot, names)) if names else set(snap.SECTIONS) - {"flakes"}
    if names == ["progress"]:
        return progress_table(shot)
    out = [blocked.line(shot.get("blocked", {}))] if shot.get("blocked") else []
    if "headline" in wanted:
        out.append(headline(shot["headline"]))
    if "needs_you" in wanted:
        out += lines("NEEDS YOU", shot["needs_you"], "nothing")
    if "attention" in wanted:
        entries = shot["attention"]
        out += lines("NEEDS ATTENTION", entries[:5], "nothing")
        remaining = max(0, shot.get("headline", {}).get("attention", len(entries)) - len(entries[:5]))
        if remaining:
            out.append(f"  and {remaining} more")
    if "pipeline" in wanted:
        out += pipeline(shot["pipeline"])
    if "agents" in wanted:
        out += agents(shot["agents"])
    if "machine" in wanted:
        out += machine(shot["machine"])
    if "cost" in wanted:
        out += cost(shot["cost"])
    if "held" in wanted:
        out += held(shot["held"], bool(names))
    if "flakes" in wanted:
        out += ["FLAKES"] + flakes([{**f, "last_seen_ago": shot["at"] - f["last_seen"]} for f in shot["flakes"]])
    return "\n".join(out)
