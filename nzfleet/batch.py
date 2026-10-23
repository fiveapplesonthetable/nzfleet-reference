"""Runs one batch to a verdict, checking one member directly or bisecting a red train."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import time

from . import agents, areas as areas_runner, config, logtail, machine, owner, plan, refresh, skips, starve, state, statuses, triage as triaging, world

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(CODE, "bin", "merge-check.sh")
RUNS = os.path.join(state.HOME, "runs")
ROUNDS = 3
FETCH_TRIES = 5
FETCH_PAUSE = 2
WALL_CLOCK_LINE = "batch wall clock expired"


def wall_clock_expired(started: float, now: float, limit: float = config.BATCH_WALL_CLOCK) -> bool:
    """Whether the batch has exhausted its configured wall clock."""
    return now - started >= limit


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(name: str, areas: list, members: list, timeout: float = 4 * 3600) -> dict:
    """One merge-check of `members` [(pr, sha)] over `areas`. Returns its result plus the log."""
    run = os.path.join(RUNS, name)
    args = [CHECK, run, str(_free_port()), ",".join(areas)] + [f"{n}:{sha}" for n, sha in members]
    os.makedirs(run, exist_ok=True)
    try:
        os.remove(os.path.join(run, "result.json"))
    except FileNotFoundError:
        pass
    process = subprocess.run(args, env=config.with_private_tmpdir(), timeout=timeout)
    try:
        with open(os.path.join(run, "result.json")) as f:
            result = json.load(f)
    except (OSError, ValueError):
        result = {"rc": process.returncode, "process_rc": process.returncode, "merged": [], "skipped": [],
                  "line": "merge-check left no result"}
    try:
        with open(os.path.join(run, "log"), errors="replace") as f:
            result["log"] = f.read()
    except OSError:
        result["log"] = ""
    waits = logtail.lock_waits(result["log"])
    result.update(tail=logtail.tail(result["log"]), lock_waits=waits, lock_holders=logtail.holders_now() if waits else [],
                  load=round(machine.load(), 1))
    shots = os.path.join(run, "shots")
    if refresh.redrawn(shots):
        result["shots"] = shots
    result["merged"] = [int(n) for n in result["merged"]]
    result["skipped"] = [int(n) for n in result["skipped"]]
    result.setdefault("skips", {})
    return result


def _heads(prs: list, sleep=time.sleep) -> dict:
    """The head of each PR, fetched with a few tries: GitHub makes `pull/N/head` a moment after it opens the PR."""
    for attempt in range(FETCH_TRIES):
        try:
            world.fetch(prs)
            return {n: world.head_of(n) for n in prs}
        except world.Failed:
            if attempt == FETCH_TRIES - 1:
                raise
            sleep(FETCH_PAUSE * 2**attempt)
    return {}


def claim_batch(prs: list, areas: list, heads: dict, main: str, group: str) -> tuple:
    """Records a running batch over `prs` unless one is already running over exactly those PRs.
    Returns (batch id, whether this call made it). The check and the insert share one lock."""
    wanted = frozenset(prs)
    with state.change() as s:
        for bid, b in s["batches"].items():
            if b["status"] == "running" and agents._alive(b["pid"]) and frozenset(int(n) for n, _ in b["members"]) == wanted:
                return bid, False
        bid = plan.new_batch_id(group, time.time(), s["batches"])
        # The claimer's pid keeps the tick from calling the record lost before the process that runs it exists.
        s["batches"][bid] = {"group": group, "members": [[n, heads[n]] for n in prs], "areas": areas, "main": main,
                             "started": time.time(), "status": "running", "pid": os.getpid()}
        starve.stop_for_batch(s)
    return bid, True


def log_of(bid: str) -> str:
    return os.path.join(state.HOME, "logs", f"batch-{bid}.log")


def _spawn(bid: str) -> int:
    """Starts `run-batch` for `bid` in a session of its own and returns its pid."""
    log = log_of(bid)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    argv = [sys.executable, "-m", "nzfleet", "run-batch", bid]
    env = config.with_private_tmpdir({**os.environ, **config.FILE})  # the machine's file wins over this shell, as it does for the daemon
    with open(log, "w") as out:
        return subprocess.Popen(machine.scoped("batch", argv), cwd=CODE, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True).pid


def start_batch(prs: list, areas: list | None = None, group: str = "manual", heads: dict | None = None, sleep=time.sleep) -> tuple:
    """The one way a batch begins: the batch already running over `prs`, or a new one. Returns (batch id, whether new).
    `heads` are the shas the caller already holds, else they are fetched; without `areas` they come from the changes."""
    heads = heads or _heads(prs, sleep)
    areas = areas if areas is not None else sorted(areas_of([heads[n] for n in prs]))
    bid, new = claim_batch(prs, areas, heads, world.main_sha(), group)
    if new:
        try:
            pid = _spawn(bid)
        except OSError:
            with state.change() as s:
                s["batches"][bid]["status"] = "lost"
            raise
        with state.change() as s:
            s["batches"][bid]["pid"] = pid
    return bid, new


def excerpt(log: str) -> str:
    lines = [line for line in log.splitlines() if any(w in line for w in ("FAILED", "panicked", "error[", "✘", "failed:", "Error:", "budget"))]
    return "\n".join(lines[:12])


def areas_of(shas: list) -> frozenset:
    from . import world
    areas = set()
    train = len(shas) > 1
    for sha in shas:
        files = world.changed_files(sha)
        areas.update(plan.check_areas(world.areas(files, "train" if train else "change"), files, config.SKIP_AREAS, train))
    return frozenset(areas)


def run(batch_id: str, check=check, areas_of=areas_of, send=None, triage=None, clock=time.monotonic) -> None:
    """Runs the batch named in state, records the verdict for each member and posts it as a commit status
    on the head that was checked. `check`, `send` and `triage` are replaceable for tests."""
    members = [(int(n), sha) for n, sha in state.read()["batches"][batch_id]["members"]]
    sender = send or statuses.GitHub.from_config()
    statuses.post_all(members, lambda pr: statuses.pending(), sender)
    try:
        _run(batch_id, check, areas_of, triage or triaging.Off(), clock)
    except BaseException:
        statuses.post_all(members, lambda pr: ("error", "the batch crashed before a verdict"), sender)
        raise
    batch = state.read()["batches"][batch_id]
    blamed = {b["pr"]: b["line"] for b in batch.get("blamed", [])}
    statuses.post_all(members, lambda pr: statuses.verdict(pr, batch["status"], batch["passed"], batch["skipped"], blamed,
                                                           batch.get("checked_areas", batch["areas"]), batch.get("skips")), sender)


def _run(batch_id: str, check, areas_of, triage, clock=time.monotonic) -> None:
    started = clock()
    batch = state.read()["batches"][batch_id]
    heads = {int(n): sha for n, sha in batch["members"]}
    order = [int(n) for n, _ in batch["members"]]
    # The areas are worked out again from the heads being checked: a stale list once let an Android change
    # through with only the secrets check.
    # Old queued batches can still carry formal; it belongs to the post-merge schedule.
    areas = sorted((set(batch["areas"]) | set(areas_of([heads[n] for n in order]))) - {"formal"})
    with state.change() as s:
        s["batches"][batch_id]["checked_areas"] = areas
        earlier = plan.reusable_batch(s["batches"], batch["members"], areas, batch["main"], batch_id)
        if earlier:
            # The same heads on the same main passed once; running them again can only reproduce that.
            before = s["batches"][earlier]
            s["batches"][batch_id].update(status="green", finished=time.time(), passed=before["passed"], skipped=before["skipped"],
                                          line=f"verdict of {earlier}, same heads on the same main", shots="", tests=[], flaky=[], stats={})
            for n in before["passed"]:
                s["passed"][str(n)] = {"sha": heads[n], "main": batch["main"], "order": before["passed"], "batch": batch_id}
            return
    def run_check(name: str, checked_areas: list, members: list) -> dict | None:
        if wall_clock_expired(started, clock()):
            _verdict(batch_id, "wall-clock", [], heads, {"rc": 124, "merged": [], "skipped": [],
                     "line": WALL_CLOCK_LINE, "log": ""})
            return None
        remaining = max(0.001, config.BATCH_WALL_CLOCK - (clock() - started))
        try:
            result = (check(name, checked_areas, members, timeout=min(4 * 3600, remaining))
                      if check is globals()["check"] else check(name, checked_areas, members))
        except subprocess.TimeoutExpired:
            if wall_clock_expired(started, clock()):
                _verdict(batch_id, "wall-clock", [], heads, {"rc": 124, "merged": [], "skipped": [],
                         "line": WALL_CLOCK_LINE, "log": ""})
                return None
            raise
        if wall_clock_expired(started, clock()):
            _verdict(batch_id, "wall-clock", [], heads, {"rc": 124, "merged": [], "skipped": [],
                     "line": WALL_CLOCK_LINE, "log": result.get("log", "")})
            return None
        return result

    for round_no in range(ROUNDS):
        members = [(n, heads[n]) for n in order]
        first = run_check(f"{batch_id}-r{round_no}", areas, members)
        if first is None:
            return
        merged = first["merged"]
        asked = triaging.Asked()
        if first["rc"] != 0 and merged:
            asked = triage.red_area(plan.failed_areas(first["line"], areas), first,
                                    {"members": len(order), "rerun_allowed": plan.reruns_on_red(state.read()["batches"], batch_id),
                                     "ledger": state.read()["flakes"]})
            if asked.escalation:
                with state.change() as current:
                    current["needs_person"][f"jev:batch:{batch_id}"] = f"Jev red-area choice for batch {batch_id} needs attention: {asked.escalation}"
                return _verdict(batch_id, "red-once", [], heads, first)
        if _not_run(first):
            triage.settle(asked, triaging.Facts(infrastructure=True))
            return _verdict(batch_id, "infrastructure", [], heads, first)
        if not merged:
            # Nothing merged means nothing was checked; a green run of main alone says nothing about these PRs.
            return _verdict(batch_id, "nothing-merged", [], heads, first)
        if first["rc"] == 0:
            proved_areas = _passed_areas(first, f"{batch_id}-r{round_no}")
            if not set(areas) <= set(proved_areas):
                missing = " ".join(sorted(set(areas) - set(proved_areas)))
                first = {**first, "line": f"incomplete proof: no successful area result for {missing}"}
                return _verdict(batch_id, "incomplete-proof", [], heads, first, proved_areas=proved_areas)
            return _verdict(batch_id, "green", merged, heads, first, proved_areas=proved_areas, check=check)
        if len(heads) == 1:
            culprit = order[0]
            triage.settle(asked, triaging.Facts(blamed=(culprit,)))
            _blame(culprit, heads[culprit], first, batch_id)
            return _verdict(batch_id, "red", [], heads, first)
        bypass_rerun = asked.action == triaging.Action.BISECT_NOW.value
        if not plan.reruns_on_red(state.read()["batches"], batch_id) and not bypass_rerun:
            return _verdict(batch_id, "red-once", [], heads, first)
        # Only failed areas rerun, so an unrelated formal suite does not repeat its full cost.
        red = plan.failed_areas(first["line"], areas)
        # The compiler gate runs alone. If it fails, rerun the full request so no
        # area skipped behind it is mistaken for a pass.
        stopped = [a for a in areas if first.get("stats", {}).get(a, {}).get("rc") == areas_runner.STOPPED]
        retry = areas if plan.GATE in red else sorted(set(red) | set(stopped))
        again = first if bypass_rerun else run_check(f"{batch_id}-r{round_no}-again", retry, members)
        if again is None:
            return
        found = triaging.Facts(rerun_green=again["rc"] == 0, still_failing=frozenset(plan.failed_tests(again["log"])),
                               load_high=(first.get("load") or 0) >= triaging.LOAD_HIGH)
        triage.settle(asked, found)
        if again["rc"] == 0 and plan.counterexample(first["log"]):
            # Random inputs make the bisection unsound: a prefix can pass by drawing other cases. A person reads it.
            return _verdict(batch_id, "counterexample", [], heads, first)
        if again["rc"] == 0:
            same_tree = first.get("tree") is not None and first.get("tree") == again.get("tree")
            proved_areas = _passed_areas(again, f"{batch_id}-r{round_no}-again")
            if same_tree:
                proved_areas = {**_passed_areas(first, f"{batch_id}-r{round_no}"), **proved_areas}
            if not set(areas) <= set(proved_areas):
                missing = " ".join(sorted(set(areas) - set(proved_areas)))
                again = {**again, "line": f"incomplete proof: no successful area result for {missing}"}
                return _verdict(batch_id, "incomplete-proof", [], heads, again, proved_areas=proved_areas)
            return _verdict(batch_id, "green", again["merged"], heads, again, proved_areas=proved_areas,
                            flaky=plan.failed_tests(first["log"]), flaky_run=os.path.join(RUNS, f"{batch_id}-r{round_no}"), check=check)
        if not plan.reruns_on_red(state.read()["batches"], batch_id):
            return _verdict(batch_id, "red-once", [], heads, again)
        probes = plan.Bisect(merged, {len(merged): False})
        while True:
            step, value = plan.next_probe(probes)
            if step != "probe":
                break
            prefix = [(n, heads[n]) for n in merged[:value]]
            probe = run_check(f"{batch_id}-r{round_no}-p{value}", plan.failed_areas(again["line"], red), prefix)
            if probe is None:
                return
            if _not_run(probe):
                return _verdict(batch_id, "infrastructure", [], heads, probe)
            probes.results[value] = probe["rc"] == 0
        if step == "main-broken":
            triage.settle(asked, dataclasses.replace(found, main_broken=True))
            return _verdict(batch_id, "main-broken", [], heads, again)
        triage.settle(asked, dataclasses.replace(found, blamed=(value,)))
        _blame(value, heads[value], again, batch_id)
        order = [n for n in order if n != value]
        if not order:
            return _verdict(batch_id, "red", [], heads, again)
    _verdict(batch_id, "gave-up", [], heads, first)


def _not_run(result: dict) -> bool:
    """merge-check exits 90 and up when it could not fetch, migrate or run at all; that says nothing about the code."""
    return "process_rc" in result or (result["rc"] >= 90 and result["rc"] != 93) or plan.classify(result["log"]) == "infrastructure"


def _passed_areas(result: dict, run: str) -> dict:
    """Areas whose runner recorded a zero exit status, excluding jobs it never started, each mapped to the run `run`."""
    return {area: run for area, stat in result.get("stats", {}).items() if stat.get("rc") == 0}


def _blame(n: int, sha: str, result: dict, batch_id: str) -> None:
    with state.change() as s:
        s["batches"][batch_id].setdefault("blamed", []).append(
            {"pr": n, "line": result["line"], "tests": plan.failed_tests(result["log"])})
        batch = s["batches"][batch_id]
        s["failed"][str(n)] = {"sha": sha, "why": result["line"], "tests": plan.failed_tests(result["log"]),
                              "areas": plan.failed_areas(result["line"], batch.get("checked_areas", batch["areas"])),
                              "log": excerpt(result["log"]), "at": time.time()}


def _excerpts(test: str, runs: list) -> list:
    out = []
    for run in runs:
        try:
            out.append((run, plan.lines_about(open(os.path.join(run, "log"), errors="replace").read(), test)))
        except OSError:
            out.append((run, ""))
    return out


def _files_of(heads: dict, passed: list) -> list:
    try:
        return [f for n in passed for f in world.changed_files(heads[n])]
    except world.Failed:
        return []


def _main_shots(shots: str, passed: list, heads: dict, batch_id: str, check) -> str:
    """Shots that moved in a train that owns none of them mean main's shots are stale or the render is not
    deterministic. Main is drawn alone once per commit, and a refresh PR opened at most once a day."""
    unowned = refresh.unowned(shots, _files_of(heads, passed), refresh.golden())
    main = world.main_sha()
    record = state.read()["stale_shots"]
    step = plan.stale_shots_step(bool(unowned), main, record, time.time())
    if step == "none":
        return f"; {len(unowned)} unowned shots, main was redrawn at {main[:8]}" + (f" (#{record['pr']})" if record.get("pr") else "") if unowned else ""
    if step == "wait":
        return f"; {len(unowned)} unowned shots, refresh #{record.get('pr')} was opened today"
    alone = check(f"{batch_id}-main", sorted(refresh.redrawn(shots)), [])
    moved = refresh.moved_paths(alone["shots"], refresh.golden()) if alone.get("shots") else []
    number = 0
    if moved:
        try:
            number = refresh.open_main_pr(alone["shots"], main, time.strftime("%Y-%m-%d"))
        except world.Failed:
            number = 0
    with state.change() as s:
        s["stale_shots"] = {"main": main, "opened": time.time() if number else record.get("opened", 0), "pr": number or record.get("pr", 0)}
    if number:
        return f"; main's own shots are stale, refresh is #{number}"
    return "; main draws its own shots as checked in, so the train moved them" if not moved else "; main's own shots are stale, no refresh opened"


def _refresh_shots(batch_id: str, passed: list, heads: dict, result: dict, check=check) -> tuple:
    """Pushes the shots the merged tree redrew as one commit on the last passed PR. Returns the batch summary's note
    and any new head that needs a fresh batch."""
    shots = result.get("shots")
    if not shots or not passed:
        return "", {}
    last = passed[-1]
    try:
        new = refresh.commit_train(heads[last], shots, batch_id, world.branch_of(last), refresh.golden())
    except world.Failed:
        new = ""
    done = f"committed on #{last}" if new else "not committed"
    stale = _main_shots(shots, passed, heads, batch_id, check)
    note = f"screenshots: {refresh.moved_summary(shots, refresh.golden())}; {done}{stale}; report: {refresh.report(shots)}"
    changed = {last: new} if new and new != heads[last] else {}
    return note, changed


def _verdict(batch_id: str, outcome: str, passed: list, heads: dict, result: dict, flaky=(), flaky_run="", proved_areas=None, check=check) -> None:
    note, shot_heads = _refresh_shots(batch_id, passed, heads, result, check) if outcome == "green" else ("", {})
    tested_heads = dict(heads)
    changed_heads = {n: sha for n, sha in shot_heads.items() if sha != tested_heads[n]}
    skipped = result.get("skipped", [])
    left_out = skips.describe(skipped, result.get("skips", {}))
    outcome = skips.outcome(outcome, skipped)
    with state.change() as s:
        batch = s["batches"][batch_id]
        s["hands"].update(skips.rebases(skipped, result.get("skips", {}), heads, s["hands"]))
        for n in passed:
            if n in changed_heads:
                if s["passed"].get(str(n), {}).get("sha") == tested_heads[n]:
                    s["passed"].pop(str(n), None)
                continue
            s["passed"][str(n)] = {"sha": tested_heads[n], "main": batch["main"], "order": passed, "batch": batch_id}
            s["failed"].pop(str(n), None)
            s["untied"].pop(str(n), None)
        if outcome in owner.UNTIED:
            for n, _ in batch["members"]:
                s["untied"][str(n)] = owner.record_untied(s["untied"].get(str(n)), heads[int(n)], result.get("line", outcome))
        final_outcome = "shots-updated" if changed_heads else outcome
        if changed_heads:
            batch.pop("proved", None)
        batch.update(status=final_outcome, finished=time.time(), line=" ".join(filter(None, [result.get("line", ""), left_out, note])), shots=note, tests=plan.failed_tests(result.get("log", "")),
                     passed=[] if changed_heads else passed, skipped=skipped, skips=result.get("skips", {}), flaky=list(flaky), stats=result.get("stats", {}), tail=result.get("tail", []), lock_holders=result.get("lock_holders", []),
                     invalidated_heads={str(n): sha for n, sha in changed_heads.items()})
        if not changed_heads and outcome in ("green", "partial", "incomplete-proof") and result.get("tree"):
            batch["proved"] = {"tree": result["tree"], "areas": sorted(proved_areas or {}), "runs": dict(sorted((proved_areas or {}).items()))}
        if flaky:
            s["flakes"] = plan.record_flakes(s["flakes"], list(flaky), flaky_run, time.time())
            taken = {t["key"] for t in s["todo"]} | set(s["agents"])
            s["todo"] += plan.flake_tasks(s["flakes"], taken, _excerpts)
