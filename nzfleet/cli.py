"""nzfleet's commands. Each is short and prints plain text a person or an agent can act on."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import time

from . import agents, batch, blocked, choice, cloud, codehost, config, db, event_log, holds, janitor, jev, journeys_run, land, machine, openpr, owner, plan, procs, progress, prove as prover, runs, selfupdate, serve as serving, setup as bootstrap, starve as starving, state, status_view, supervise_tick, tick, tidy as tidying, top as topview, train, triage as triaging, trainrun, units, warm, watchdog, work, world


def _files(files: list) -> str:
    shown = ", ".join(files[:4])
    return shown + (f" and {len(files) - 4} more" if len(files) > 4 else "")


def _age(seconds: float) -> str:
    minutes = int(seconds / 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _standing() -> tuple:
    """The running commit, main's, and the nzfleet commits the running code lacks; blanks when git cannot say."""
    checkout = codehost.Checkout()
    return checkout.head(), checkout.main(), checkout.behind()


def version(_args) -> int:
    head, main, behind = _standing()
    moved = state.read()["code_moved"]
    print(f"running {head[:8]} from {codehost.CODE}")
    print(f"main is {main[:8]}; {selfupdate.distance(behind, time.time())}")
    print("last move: " + (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(moved['at']))} from {moved['from'][:8]} to {moved['to'][:8]}" if moved else "none recorded"))
    print(selfupdate.RULE)
    reinstalled = units.sync()
    print("units: " + (f"{', '.join(reinstalled)} differed from bin/ and were reinstalled"
                       + ("; restart to apply: systemctl --user restart nzfleet.service" if units.SERVICE in reinstalled else "")
                       if reinstalled else "match bin/"))
    return 0


def warn_if_behind() -> None:
    """One line on stderr per run when the code is behind main's tools/nzfleet. Silent where git cannot say."""
    try:
        line = selfupdate.warning(codehost.Checkout().behind(), time.time())
    except (OSError, ValueError, world.Failed):
        return
    if line:
        print(line, file=sys.stderr)


def status(args) -> int:
    return status_view.run(args.section or [], args.json)


def next_step(_args) -> int:
    """The single most useful thing to do now, as one instruction. Work an agent already holds is skipped."""
    s = state.read()
    try:
        open_prs = world.open_prs()
        failed = tick.current_failures(s["failed"], open_prs)
        hands = tick.current_hands(s["hands"], open_prs)
    except world.Failed:
        failed = {}
        hands = {}
    held = {key for key, a in s.get("agents", {}).items() if a["status"] == "running"}
    actionable_hands = [(n, h) for n, h in sorted(hands.items()) if f"hands-{n}" not in held]
    if actionable_hands:
        n, h = actionable_hands[0]
        print(f"Rebase PR #{n} onto its base as plain commits; it conflicts in {_files(h['files'])}. Push, then: nzfleet tick")
    elif failed:
        n, f = sorted(failed.items())[0]
        print(f"Fix PR #{n}: its batch failed with: {f['why']}. Tests: {', '.join(f.get('tests', [])) or 'see nzfleet status'}. Push to its branch, then: nzfleet tick")
    elif s["notified"]:
        print(f"Ask the owner to land: {plan.land_command(train.open_for(s['trains'], s['notified']))}")
    elif stale := holds.stale_holds(s["holds"], s["todo"], time.time()):
        key = stale[0]
        print(f"decide: release or retire {key}, held for {_age(time.time() - s['holds'][key]['since'])}: {s['holds'][key]['why']}. "
              f"nzfleet release {key}  or  nzfleet retire {key} --why \"...\"")
    elif held or any(b["status"] == "running" for b in s["batches"].values()):
        print("Agents or batches are working. Check again later with: nzfleet status")
    else:
        print("Nothing is waiting. Run: nzfleet tick")
    return 0


def run_tick(args) -> int:
    tick.run(dry_run=args.dry_run)
    return 0


def poll(last_tick: float, last_main: str, now: float, run=None) -> tuple:
    """One look at GitHub: ticks at once when something happened or main moved, else once a minute. Returns the new (last_tick, last_main)."""
    happened = False
    try:
        happened = tick.watch()
    except world.Failed as e:
        tick.say(f"watch failed: {e}")
    main = last_main
    try:
        main = world.main_remote()
    except world.Failed as e:
        tick.say(f"reading main failed: {e}")
    if owner.main_moved(last_main, main):
        tick.say(f"main moved to {main[:8]}")
    if happened or owner.main_moved(last_main, main) or now - last_tick >= 60:
        (run or tick.run)()
        return now, main
    return last_tick, main


def daemon(_args) -> int:
    """Looks at GitHub every 15 seconds, ticks as soon as something happens or main moves, and every minute regardless."""
    if units.SERVICE in units.sync():
        codehost.restart_service()
        return 0
    why = blocked.missing_licence(os.environ)
    if why:
        print(f"daemon cannot start agents: {why}", file=sys.stderr)
        return 1
    last_tick, last_main = 0.0, ""
    while True:
        ticked, last_main = poll(last_tick, last_main, time.time())
        if ticked != last_tick:
            watchdog.heartbeat()
            last_tick = ticked
            # A batch reads bin/merge-check.sh as it runs, so the checkout moves only between batches.
            running = any(b["status"] == "running" for b in state.read()["batches"].values())
            if not running:
                note = selfupdate.advance(codehost.Checkout())
                if note:
                    tick.say(note)
        time.sleep(15)


def events(args) -> int:
    """Prints the event log (merges, comments, conflicts, PRs opened and closed, ready lists, deploys) and exits; follows only with -f."""
    try:
        since = event_log.parse_since(args.since, datetime.datetime.now(datetime.timezone.utc)) if args.since else None
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        with open(tick.EVENTS) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in event_log.select(lines, since, args.last):
        print(line)
    if args.follow:
        os.execvp("tail", ["tail", "-n", "0", "-F", tick.EVENTS])
    return 0


def top(_args) -> int:
    return topview.run()


def serve(args) -> int:
    return serving.run(args.port)


def watch_daemon(_args) -> int:
    print(watchdog.check())
    print(watchdog.root_disk())
    return 0


def run_batch(args) -> int:
    batch.run(args.id, triage=triaging.live())
    return 0


def manual_batch(args) -> int:
    areas = args.areas.split(",") if args.areas else None
    if not args.foreground:
        bid, new = batch.start_batch(args.prs, areas)
        print(f"{'started' if new else 'already running'}; the verdict is in `nzfleet status` and the log is {batch.log_of(bid)}")
        return 0
    heads = batch._heads(args.prs)
    bid, new = batch.claim_batch(args.prs, areas if areas is not None else sorted(batch.areas_of([heads[n] for n in args.prs])),
                                 heads, world.main_sha(), "manual")
    if not new:
        print(f"already running as {bid}; its verdict is in `nzfleet status`")
        return 0
    batch.run(bid, triage=triaging.live())
    b = state.read()["batches"][bid]
    print(f"{b['status']}: passed {b.get('passed', [])}; {b.get('line', '')}")
    return 0 if b["status"] == "green" else 1


def replay(args) -> int:
    from . import shape
    world.fetch([args.pr])
    prs = tick.read()
    up = plan.parents(prs, world.is_ancestor)
    parent = up.get(args.pr)
    base = world.head_of(parent) if parent else world.main_sha()
    sha = world.head_of(args.pr)
    outcome, detail = shape.replay(args.pr, base, args.dry_run)
    if not args.dry_run:
        tick.settle_replay(args.pr, sha, parent, outcome, detail, base)
    print(outcome, detail or "")
    return 0 if outcome != "hands" else 1


def todo(args) -> int:
    """Lists the work queue, or adds a task to it."""
    if args.key:
        after = [k for k in (args.after or "").split(",") if k]
        problem = next((p for p in map(holds.after_problem, after) if p), "")
        if problem:
            print(problem)
            return 1
        task = open(args.task[0]).read() if len(args.task) == 1 and os.path.isfile(args.task[0]) else " ".join(args.task)
        try:
            chosen = choice.resolve(args.provider, args.model, config.CODEX_MODEL)
            workers = choice.parse_compare(args.compare, config.CODEX_MODEL) if args.compare else []
        except choice.Unknown as e:
            print(e, file=sys.stderr)
            return 1
        if args.compare and (len(workers) < 2 or chosen):
            print("--compare takes two or more provider:model workers and replaces --provider and --model", file=sys.stderr)
            return 1
        items = choice.compare_items(args.key, task, args.area or "", after, workers) if workers else [
            {"key": args.key, "task": task, "model": chosen[1] if chosen else "sonnet", "area": args.area or "", "after": after,
             "kind": "task", "pr": None,
             **({"provider": chosen[0]} if chosen else {}), **({"now": True} if args.now else {})}]
        with state.change() as s:
            keys = {i["key"] for i in items}
            s["todo"] = [t for t in s["todo"] if t["key"] not in keys] + items
        print("queued " + ", ".join(f"{i['key']} ({i['provider']}/{i['model']})" if i.get("provider") else i["key"] for i in items))
        return 0
    s = state.read()
    if args.retired:
        for line in holds.retired_lines(s["retired"]) or ["nothing retired"]:
            print(f"  {line}")
        return 0
    agents_now = s.get("agents", {})
    for t in s["todo"]:
        status = agents_now.get(t["key"], {}).get("status") or ("held" if t["key"] in s["holds"] else "waiting")
        after = f" after {','.join(t['after'])}" if t.get("after") else ""
        area = f" [{t['area']}]" if t.get("area") else ""
        print(f"  {t['key']:28} {status:8} {choice.label(t):24}{area}{after}  {t['task'][:70]}")
    return 0


def agent_session(args) -> int:
    """Selects or clears the provider/model shared by CLI and daemon dispatch."""
    if args.clear:
        with state.change() as s:
            s["agent_session"] = {}
        print("agent session cleared; dispatch uses NZFLEET_AGENT")
        return 0
    if not args.name or not args.provider or (not args.model and args.provider != "codex"):
        print("set a name and --provider; set --model except for Codex, or use --clear", file=sys.stderr)
        return 2
    model = args.model or config.CODEX_MODEL
    with state.change() as s:
        s["agent_session"] = {"name": args.name, "provider": args.provider, "model": model}
    print(f"agent session {args.name}: {args.provider} {model}")
    return 0


def hold(args) -> int:
    """Stops a queued task, a hands rebase, a running agent's resumption or a batch group from starting until it is released."""
    target = runs.base_key(args.target)
    with state.change() as s:
        problem = holds.hold_problem(target, args.why or "", s["todo"], s["holds"], s["agents"])
        if not problem:
            s["holds"] = holds.with_hold(s["holds"], target, args.why, time.time())
    print(problem or f"holding {target}")
    return 1 if problem else 0


def retire(args) -> int:
    """Takes a queued task whose work is done or superseded off the queue, and records why."""
    with state.change() as s:
        problem = holds.retire_problem(args.key, args.why or "", s["todo"], s["agents"])
        if not problem:
            s["todo"], s["holds"], s["retired"] = holds.retire(s["todo"], s["holds"], s["retired"], args.key, args.why, time.time())
    print(problem or f"retired {args.key}")
    return 1 if problem else 0


def release(args) -> int:
    """Lets a held task or batch group start again."""
    with state.change() as s:
        problem = holds.release_problem(args.target, s["holds"])
        if not problem:
            s["holds"] = holds.without_hold(s["holds"], args.target)
    print(problem or f"released {args.target}")
    return 1 if problem else 0


def attach(args) -> int:
    """Records the PR a finished task produced, when nzfleet could not tell which it was."""
    number = int(args.pr.lstrip("#"))
    try:
        branch = world.branch_of(number)
    except world.Failed as e:
        print(f"could not read PR #{number}: {e}")
        return 1
    with state.change() as s:
        agent = s.get("agents", {}).get(args.key)
        if agent is None:
            print(f"no agent run is recorded for {args.key}")
            return 1
        agent["pr"] = {"number": number, "branch": branch, "how": "attached"}
        item = next((t for t in s.get("todo", []) if t["key"] == args.key), None)
        if item and item.get("area") and not agent.get("area"):
            agent["area"] = item["area"]
    print(f"{args.key} produced #{number} ({branch})")
    return 0


def note(args) -> int:
    """Records the phase an agent is in, so `nzfleet progress` can say what it is doing and for how long."""
    parsed = progress.parse_note(args.text)
    if parsed is None:
        print(f"write the note as `<phase>: <one line>`, phase being one of {', '.join(p.value for p in progress.Phase)}, "
              f"the line under {progress.NOTE_LIMIT} characters")
        return 1
    key = runs.base_key(args.key)
    with state.change() as s:
        s["notes"][key] = progress.with_note(s["notes"].get(key, []), parsed, time.time())
    print(f"noted for {key}: {parsed.phase.value}")
    return 0


def show_progress(_args) -> int:
    return status_view.run(["progress"], False)


def starve(args) -> int:
    """Runs a command with burners on at most one eighth of fleet CPUs, for no more than five minutes."""
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("nothing to run. Usage: nzfleet starve [--slice N] [--cpu-set 0-3] -- <command...>")
        return 1
    available = config.available_cpus()
    plan_ = starving.plan_starve(len(available), args.slice, args.cpu_set, shutil.which("taskset") is not None,
                                 shutil.which("systemd-run") is not None and bool(os.environ.get("XDG_RUNTIME_DIR")), available)
    return starving.run(command, plan_)


def flakes(_args) -> int:
    return status_view.run(["flakes"], False)


def start(args) -> int:
    return work.start(args.slug, args.base)


def prove(_args) -> int:
    return prover.prove()


def wait(args) -> int:
    return procs.wait(args.target, procs.root())


def run(args) -> int:
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        print("nothing to run. Usage: nzfleet run <name> -- <command...>")
        return 1
    wt = procs.root()
    procs.load_env(wt)
    why = procs.start(args.name, argv, wt)
    print(why or f"started {args.name}. Wait: nzfleet wait {args.name}   Log: {wt}/.nzrun/{args.name}.log   Stop: nzfleet kill {args.name}")
    return 1 if why else 0


def kill(args) -> int:
    why = procs.kill(args.name, procs.root(), now=args.now)
    print(why or f"{args.name} is stopped")
    return 1 if why else 0


def stop(args) -> int:
    why = agents.stop(args.key, time.time())
    print(why or f"{args.key} is stopped and will not be resumed")
    return 1 if why else 0


def pr(args) -> int:
    return openpr.pr(args.title, args.message or "", args.dry_run, args.shots_anyway)


def database(args) -> int:
    try:
        if args.remove:
            db.remove(args.name)
        else:
            print(f"export DATABASE_URL={db.up(args.name, args.tree or os.getcwd())}")
    except world.Failed as e:
        print(f"agent-db: {e}", file=sys.stderr)
        return 1
    return 0


def tidy(args) -> int:
    if args.stale_runs is not None:
        if args.stale_runs < 1:
            print("--stale-runs must be at least 1 day", file=sys.stderr)
            return 2
        try:
            s = state.read()
            prs = world.open_prs()
            open_prs = {int(pr["number"]) for pr in prs} | {pr.get("headRefName", "") for pr in prs}
        except world.Failed as e:
            print(f"could not read open PRs: {e}", file=sys.stderr)
            return 1
        candidates = {}
        for key, run in s["agents"].items():
            if run.get("status") not in ("unfinished", runs.ABANDONED):
                continue
            path = run.get("worktree") or next((w["path"] for w in s["worktrees"].values()
                                                 if w.get("agent") == runs.base_key(key)), "")
            branch = world.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False).strip() if path and os.path.isdir(path) else ""
            candidates[key] = {**run, "branch": branch}
        live = {key for key, run in s["agents"].items() if run.get("pid") and agents._alive(run["pid"])}
        retired = tidying.stale_runs(candidates, time.time(), args.stale_runs, live, open_prs)
        if args.dry_run:
            for key in retired or ["no stale runs"]:
                print(f"would retire {key}" if key != "no stale runs" else key)
            return 0
        retired_now = []
        with state.change() as current:
            for key in retired:
                agent = current["agents"].get(key)
                if agent and agent.get("status") == "unfinished" and not (agent.get("pid") and agents._alive(agent["pid"])):
                    agent["status"] = "retired"
                    agent["retired"] = time.time()
                    if agent.get("unit"):
                        machine.stop_unit(agent["unit"])
                    retired_now.append(key)
                suffix = key.removeprefix("hands-")
                if key in retired_now and key.startswith("hands-") and suffix.isdigit():
                    current["hands"].pop(suffix, None)
            retired_keys = {runs.base_key(key) for key in retired_now}
            remaining = []
            for item in current["todo"]:
                if item["key"] in retired_keys:
                    current.setdefault("task_history", {})[item["key"]] = {**item, "status": "retired", "why": "stale run"}
                else:
                    remaining.append(item)
            current["todo"] = remaining
        print(f"retired {len(retired_now)} stale run(s)" if retired_now else "no stale runs")
        return 0
    for line in janitor.run(janitor.Host(list(config.BUILD_ROOTS), config.WORK, config.REPO, tmp=config.tmp_root(), state=state.read(),
                        caches=janitor.caches(config.HOME, config.WARM, config.SCCACHE, config.CACHE, config.WORK), jev_act=config.JEV_ACT), args.dry_run) or ["nothing to tidy"]:
        print(line)
    return 0


def machine_setup(_args) -> int:
    return bootstrap.setup(os.path.expanduser("~"))


def summary_line(args) -> int:
    print(plan.summary_line(open(args.log, errors="replace").read(), args.areas))
    return 0


def shrink_warm(args) -> int:
    for name in warm.shrink(args.target, args.max_gb):
        print(f"warm: dropped {name}, the least recently used area")
    return 0


def queue(_args) -> int:
    s = state.read()
    print(plan.land_command(train.open_for(s["trains"], s["notified"])) or "nothing ready")
    return 0


def train_prs(args) -> int:
    generators = json.load(open(config.GENERATORS))
    try:
        return trainrun.run(args.prs, generators, args.max)
    except world.Failed as e:
        print(f"train: {e}", file=sys.stderr)
        return 1


def release_train(args) -> int:
    try:
        return trainrun.release(args.pr)
    except world.Failed as e:
        print(f"train release: {e}", file=sys.stderr)
        return 1


def land_train(args) -> int:
    return land.land(args.train)


def journeys(args) -> int:
    sha = args.sha or world.main_sha()
    if not journeys_run.settings().serial:
        print("journeys: NZFLEET_JOURNEYS_SERIAL names no phone", file=sys.stderr)
        return 1
    return journeys_run.run(journeys_run.Host(), tick.tell_event, sha)


def main(argv) -> int:
    p = argparse.ArgumentParser(
        prog="nzfleet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Moves nzela PRs from draft to deployed: batches them, bisects a red batch, tells the owner what to\n"
                    "merge (issue #811), reports test deployments, and starts agents for queued work.\n\n" + selfupdate.RULE,
        epilog="If you are an agent doing a task: `nzfleet start <slug>`, work, `nzfleet prove`, `nzfleet pr \"<title>\" -m \"...\"`.\n"
               "Anything over two minutes: `nzfleet run <name> -- cmd`, then `nzfleet wait <name>` (exit 3 means call again).\n"
               "If you are told what is next: run `nzfleet next` and do what it says, then `nzfleet tick`.\n"
               "Push PR branches as plain commits on main (or the PR they stack on). Never merge; never apply Terraform.\n"
               "Owner asks go on issue #695. News: `nzfleet events --since 1h` (add -f to follow). Live view: `nzfleet top`.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="the commit nzfleet runs, main's, how far behind it is, and the last move").set_defaults(fn=version)
    st = sub.add_parser("status", help="everything on one screen, from the last tick's snapshot", description=status_view.SCHEMA,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    st.add_argument("--json", action="store_true", help="the snapshot as JSON")
    st.add_argument("--section", action="append", help="only this section (repeatable); needs, progress and flakes are aliases")
    st.set_defaults(fn=status)
    sub.add_parser("next", help="the one thing to do next").set_defaults(fn=next_step)
    t = sub.add_parser("tick", help="one pass of the loop")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=run_tick)
    sub.add_parser("daemon", help="tick every minute").set_defaults(fn=daemon)
    b = sub.add_parser("batch", help="check these PRs together now, detached; --foreground waits and prints the verdict")
    b.add_argument("--foreground", action="store_true")
    b.add_argument("prs", nargs="+", type=int)
    b.add_argument("--areas")
    b.set_defaults(fn=manual_batch)
    rb = sub.add_parser("run-batch")
    rb.add_argument("id")
    rb.set_defaults(fn=run_batch)
    sl = sub.add_parser("summary-line")
    sl.add_argument("log")
    sl.add_argument("areas", nargs="+")
    sl.set_defaults(fn=summary_line)
    sw = sub.add_parser("shrink-warm", help="drop the least recently used areas of a build directory until it fits")
    sw.add_argument("target")
    sw.add_argument("max_gb", type=int)
    sw.set_defaults(fn=shrink_warm)
    r = sub.add_parser("replay", help="rewrite a PR as its own commits on its base")
    r.add_argument("pr", type=int)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=replay)
    sub.add_parser("queue", help="the land command").set_defaults(fn=queue)
    tr = sub.add_parser("train", help="merge open PRs into one pull request and compile it; mail waits for checks and `release-train`")
    tr.add_argument("prs", nargs="*", type=int, help="default: every open, unheld PR whose task is done")
    tr.add_argument("--max", type=int, help="at most this many PRs, in train order")
    tr.set_defaults(fn=train_prs)
    rl = sub.add_parser("release-train", help="a reviewer has read this train's current head; mail the merge command once its checks are complete")
    rl.add_argument("pr", type=int)
    rl.set_defaults(fn=release_train)
    ld = sub.add_parser("land", help="merge a verified train in order, if main is still where it was checked")
    ld.add_argument("train")
    ld.set_defaults(fn=land_train)
    w = sub.add_parser("todo", help="the work queue agents take from; with a key, add to it")
    w.add_argument("key", nargs="?")
    w.add_argument("task", nargs="*", help="the brief, or a file holding it")
    w.add_argument("--model", help="sonnet, haiku, opus, claude-* or gpt-6-luna, gpt-6-sol; the model name implies the provider")
    w.add_argument("--provider", choices=("codex", "claude"), help="run this task on this provider whatever the session chose")
    w.add_argument("--compare", help="provider:model,provider:model: queue the brief once per worker, each on its own branch and draft PR")
    w.add_argument("--after", help="task keys that must finish first, or pr:<n> for a PR that must have merged, comma separated")
    w.add_argument("--area", help="a short name for the part of the code; items in one area run one at a time, each on the last one's PR")
    w.add_argument("--now", action="store_true", help="the task unblocks merges: it starts on the next tick, ahead of the queue and outside its area's lane")
    w.add_argument("--retired", action="store_true", help="list the tasks that were retired instead")
    w.set_defaults(fn=todo)
    ag = sub.add_parser("session", help="select the provider and model for this fleet session; CLI and daemon share it")
    ag.add_argument("name", nargs="?", help="a label for this nzfleet session")
    ag.add_argument("--provider", choices=("codex", "claude", "configured"), help="codex uses NZFLEET_AGENT_CODEX; others use NZFLEET_AGENT")
    ag.add_argument("--model", help="exact model identifier; Codex defaults to NZFLEET_CODEX_MODEL")
    ag.add_argument("--clear", action="store_true", help="return to the generic NZFLEET_AGENT selection")
    ag.set_defaults(fn=agent_session)
    h = sub.add_parser("hold", help="stop a queued task or a batch group (core, android, console, light, wide) from starting")
    h.add_argument("target", help="a task key or a batch group")
    h.add_argument("--why", help="what it waits for; shown in status")
    h.set_defaults(fn=hold)
    rt = sub.add_parser("retire", help="take a held task whose work is done or superseded off the queue")
    rt.add_argument("key")
    rt.add_argument("--why", help="what made the work unnecessary; kept in the retired list")
    rt.set_defaults(fn=retire)
    rl = sub.add_parser("release", help="let a held task or batch group start again")
    rl.add_argument("target")
    rl.set_defaults(fn=release)
    at = sub.add_parser("attach", help="record the PR a finished queued task produced")
    at.add_argument("key")
    at.add_argument("pr")
    at.set_defaults(fn=attach)
    nt = sub.add_parser("note", help="say what phase you are in: nzfleet note <key> \"test: running the store tests\"")
    nt.add_argument("key", help="the task key in your brief")
    nt.add_argument("text", help="<read|write|test|fix|push>: <one line>")
    nt.set_defaults(fn=note)
    sub.add_parser("progress", help="each agent's last commit, change and test, phase and CPU; each chain's steps, waits and head").set_defaults(fn=show_progress)
    sv = sub.add_parser(
        "starve", help="bounded CPU starvation run",
        description="Run a command for at most five minutes on at most one eighth of fleet CPUs. "
                    "Refuse while a merge-check batch is running; stop burners when one starts.")
    sv.add_argument("--slice", type=int, default=starving.SLICE, help="CPUs in the slice, capped at one eighth of the fleet (default 4)")
    sv.add_argument("--cpu-set", help="the CPUs to use instead, capped at one eighth of the fleet, e.g. 0-3")
    sv.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    sv.set_defaults(fn=starve)
    sub.add_parser("flakes", help="tests that failed and passed on rerun, by count").set_defaults(fn=flakes)
    st = sub.add_parser("start", help="a worktree, branch, ports, database and node_modules of your own")
    st.add_argument("slug", help="what becomes true, as the branch name: a-held-rate-can-be-discarded")
    st.add_argument("--base", default="main", help="main, or the number of the PR this one stacks on")
    st.set_defaults(fn=start)
    sub.add_parser("prove", help="run the checks this change calls for, here, in the foreground").set_defaults(fn=prove)
    wt = sub.add_parser("wait", help="wait up to 9 minutes for a process: exit 0 it ended, 3 call again")
    wt.add_argument("target", help="a name given to `run`, or a pid")
    wt.set_defaults(fn=wait)
    rn = sub.add_parser("run", help="start a command detached, under a name, with a log")
    rn.add_argument("name")
    rn.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    rn.set_defaults(fn=run)
    k = sub.add_parser("kill", help="stop a process `run` started, and what it started")
    k.add_argument("name")
    k.add_argument("--now", action="store_true", help="SIGKILL at once, for a process that ignores SIGTERM")
    k.set_defaults(fn=kill)
    sp = sub.add_parser("stop", help="stop a worker for good: its processes end and it is not resumed")
    sp.add_argument("key")
    sp.set_defaults(fn=stop)
    pp = sub.add_parser("pr", help="push the branch and open or update its draft PR")
    pp.add_argument("title", help="what is now true, present tense, no prefix")
    pp.add_argument("-m", "--message", help="what changed and why, a few sentences")
    pp.add_argument("--dry-run", action="store_true", help="check and print the PR without pushing")
    pp.add_argument("--shots-anyway", action="store_true", help="allow screenshots of screens this diff does not touch")
    pp.set_defaults(fn=pr)
    d2 = sub.add_parser("db", help="a Postgres of your own with every migration applied (what agent-db calls)")
    d2.add_argument("name")
    d2.add_argument("tree", nargs="?", help="the checkout whose migrations to apply (default: here)")
    d2.add_argument("--rm", dest="remove", action="store_true", help="remove it")
    d2.set_defaults(fn=database)
    td = sub.add_parser("tidy", help="remove idle build output, containers and worktrees now (the daemon does this every 15 minutes)")
    td.add_argument("--dry-run", action="store_true")
    td.add_argument("--stale-runs", type=int, help="retire unfinished runs older than this many days when no process or open PR remains")
    td.set_defaults(stale_runs=None)
    td.set_defaults(fn=tidy)
    sub.add_parser("setup", help="bring this machine to what nzfleet needs, and list what is missing; safe to repeat").set_defaults(fn=machine_setup)
    sub.add_parser("watchdog", help="open, update or close the 'nzfleet is down' issue; run by nzfleet-watchdog.timer").set_defaults(fn=watch_daemon)
    ev = sub.add_parser("events", help="merges, comments, conflicts and deploys, newest last; exits unless -f")
    ev.add_argument("--since", help="only events after this: 30m, 2h, 1d, or a UTC time such as '2026-10-10 17:00'")
    ev.add_argument("-n", "--last", type=int, default=20, help="without --since, how many of the latest (default 20)")
    ev.add_argument("-f", "--follow", action="store_true", help="then keep following new events")
    ev.set_defaults(fn=events)
    sv = sub.add_parser("serve", help="status.html on the tailnet, read-only; refuses to start without a Tailscale address")
    sv.add_argument("--port", type=int, default=config.SERVE_PORT)
    sv.set_defaults(fn=serve)
    jv = sub.add_parser("jev", help="the decisions Jev was asked, and how they compared with the rules")
    jvs = jv.add_subparsers(dest="jev_cmd", required=True)
    jr = jvs.add_parser("replay", help="re-run a logged decision against the current rule")
    jr.add_argument("id")
    jr.add_argument("--live", action="store_true", help="also ask Jev again with the logged state")
    jr.set_defaults(fn=lambda a: jev.replay_command(a))
    jp = jvs.add_parser("report", help="per decision point: agreement with the rule, confidence, disagreements and what became of each side")
    jp.add_argument("--since", help="only decisions after this: 30m, 2h, 7d, or a UTC time")
    jp.add_argument("--until", help="only decisions before this, in the same forms")
    jp.add_argument("--point", help="only this decision point, for example batch.red_action")
    jp.add_argument("--disagreements", type=int, default=10, help="how many disagreements to list per point")
    jp.set_defaults(fn=lambda a: jev.report_command(a))
    jy = sub.add_parser("journeys", help="the phone journeys the daemon runs when main moves onto the app")
    jys = jy.add_subparsers(dest="journeys_cmd", required=True)
    jyr = jys.add_parser("run", help="run them now on this commit, in the foreground; the daemon calls this detached")
    jyr.add_argument("sha", nargs="?")
    jyr.set_defaults(fn=journeys)
    sub.add_parser("top", help="the status sections redrawn every few seconds, newest events at the bottom; q quits").set_defaults(fn=top)
    args = p.parse_args(argv)
    if args.cmd not in ("run-batch", "summary-line", "shrink-warm"):
        warn_if_behind()
    sys.stdout.reconfigure(line_buffering=True)  # a progress line must reach a pipe when it is printed, not when the command ends
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
