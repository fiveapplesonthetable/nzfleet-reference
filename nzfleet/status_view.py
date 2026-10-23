"""`nzfleet status`: the last snapshot the daemon wrote, rendered. Nothing here asks GitHub, git or the process table."""

from __future__ import annotations

import json
import sys
import time

from . import render, snapshot, snapshot_tick

MAX_AGE = 300.0  # seconds; the daemon writes every tick, so an older snapshot means it stopped

SCHEMA = f"""\
status reads $NZFLEET_HOME/snapshot.json, which the daemon rewrites every tick, so it answers at once and is as old as the last tick. A snapshot older than 5 minutes is refused, not shown.
Sections, in order: {', '.join(snapshot.SECTIONS)}. Aliases: needs (needs_you, attention), progress (agents, pipeline), flakes.
--json prints the snapshot (schema version {snapshot.VERSION}), or only the sections named by --section, as one object:
  version, at                      schema version; when the snapshot was taken, epoch seconds
  headline                         main_status, main_green, main_last_completed_green, main_last_completed_for, deploy_age, ready, load, cores, usd_hour, codex_hour, needs_you, attention
  needs_you, attention             lists of {{kind, subject, text, fix}}; fix is the command that resolves it, or empty
  pipeline                         queue, batches, trains, train_prs, main (current sha/status/conclusion/run and last completed outcome/age), chains, waiting
  agents                           one row per run: key, state, provider, model, age, since_commit, since_change, cores, turns, context, usd, phase, note
  machine                          load, cores, mem_total_gb, mem_free_gb, temp_c, disk, cpu_by_owner {{owner: {{cores, quota}}}}
  cost                             hour, today, week, by_model, by_place, top_today, per_pr
  held                             holds {{key, why, age, stale}}, refused {{key, reason}}
  flakes                           {{test, count, last_seen}}
Ages are seconds. A section the snapshot lacks is an error, not an empty answer."""


def run(sections: list, as_json: bool, path: str = snapshot_tick.PATH, now: float | None = None) -> int:
    try:
        with open(path) as f:
            shot = json.load(f)
    except (OSError, ValueError):
        print("no snapshot yet; the daemon writes one every tick, or run: nzfleet tick")
        return 1
    unknown = [n for n in sections if n not in snapshot.SECTIONS and n not in snapshot.ALIASES]
    if unknown:
        print(f"unknown section {', '.join(unknown)}; one of {', '.join(snapshot.SECTIONS)}, needs, progress", file=sys.stderr)
        return 2
    age = (now if now is not None else time.time()) - shot["at"]
    if age > MAX_AGE:
        print(f"the snapshot is {int(age // 60)} min old and the daemon writes one every tick; it has stopped. run: nzfleet tick")
        return 1
    if as_json:
        print(json.dumps({"version": shot["version"], "at": shot["at"], **(snapshot.section(shot, sections) if sections else shot)}, indent=1))
        return 0
    text = render.text(shot, sections)
    if shot.get("blocked") and sys.stdout.isatty():
        first, _, rest = text.partition("\n")
        text = f"\033[31m{first}\033[0m\n{rest}"
    print(text)
    print(f"\n(snapshot from {int(age)} s ago)")
    return 0
