"""One pass of the loop: read, shape, batch, ready, tell, deploy, watch main, answer comments, keep the machine tidy."""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time

from . import agents, batch as batching, checkout, cloud, config, fixes, holds, janitor, jev, jev_points, journeys_run, landed as landed_mod, machine, mail, owner, owner_tick, plan, procs, rebases, runs, shotsfix, snapshot_tick, state, supervise_tick, task_queue, tidy as tidying, train, trainrun, unbreak, world

QUEUE_TITLE = "Merge queue"
BATCH_SLOTS = 2
LOG = os.path.join(state.HOME, "logs", "tick.log")


def say(line: str) -> None:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"{stamp} {line}", flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{stamp} {line}\n")


RECENT = 20 * 60


def current_items(items: dict, open_prs: list) -> dict:
    """Recorded PR items whose pull request remains open at the exact recorded head."""
    heads = {str(pr["number"]): pr["headRefOid"] for pr in open_prs}
    return {n: item for n, item in items.items() if heads.get(n) == item.get("sha")}


def current_failures(failed: dict, open_prs: list) -> dict:
    return current_items(failed, open_prs)


def current_hands(hands: dict, open_prs: list) -> dict:
    return current_items(hands, open_prs)


def retire_items(st: dict, gone: dict) -> None:
    """Takes the queued items whose PR is in `gone` off the queue, keeping each with the reason."""
    st["todo"], withdrawn = task_queue.withdraw(st["todo"], gone)
    for item, why in withdrawn:
        st.setdefault("task_history", {})[item["key"]] = {**item, "status": "retired", "why": why}


def retire_closed_afters(standing: dict) -> None:
    """Takes off the queue each task that waits with `--after pr:<n>` on a PR closed without landing: it can never start."""
    with state.change() as st:
        gone = {t["key"]: n for t in st["todo"] if (n := holds.closed_after(t.get("after", []), standing)) is not None}
        for item in [t for t in st["todo"] if t["key"] in gone]:
            st["todo"].remove(item)
            st.setdefault("task_history", {})[item["key"]] = {**item, "status": "retired",
                                                                "why": f"#{gone[item['key']]} was closed without landing on main"}


def retire_rebase_work(members: set | list) -> None:
    """Removes and stops rebase work whose PR has landed, including a hand already running."""
    numbers = {int(n) for n in members}
    if not numbers:
        return
    agents_now = state.read().get("agents", {})
    for number in numbers:
        key = f"hands-{number}"
        agent = agents_now.get(key, {})
        pid = agent.get("pid")
        if agent.get("status") == "running" and pid and procs.alive(pid):
            try:
                if os.getpgid(pid) == pid:
                    procs.kill_group(pid)
            except OSError:
                pass
    with state.change() as st:
        retire_items(st, {n: "PR merged" for n in numbers})
        for number in numbers:
            st["hands"].pop(str(number), None)
            key = f"hands-{number}"
            if key in st["agents"] and st["agents"][key].get("status") == "running":
                st["agents"][key].update(status="retired", finished=time.time(), summary="PR merged")
                if st["agents"][key].get("unit"):
                    machine.stop_unit(st["agents"][key]["unit"])


def prune_stale(open_prs: list, merged_prs: set | None = None) -> None:
    """Remove failure and replay records for closed PRs or heads that have moved."""
    s = state.read()
    if merged_prs:
        retire_rebase_work(merged_prs)
        s = state.read()
    open_numbers = {pr["number"] for pr in open_prs}
    merged_members = set(merged_prs or ()) | landed_mod.landed_members(s["train_prs"], merged_prs or set())
    if len(open_prs) < world.OPEN_LIMIT:
        gone = {item["pr"]: "PR merged" if item["pr"] in merged_members else "PR closed"
                for item in s["todo"] if item["pr"] is not None and item["pr"] not in open_numbers}
        if gone:
            with state.change() as st:
                retire_items(st, gone)
    failed = current_failures(s["failed"], open_prs)
    hands = current_hands(s["hands"], open_prs)
    untied = current_items(s["untied"], open_prs)
    attempts = {n: r for n, r in s["fixes"].items() if n in {str(pr["number"]) for pr in open_prs}}
    if failed == s["failed"] and hands == s["hands"] and untied == s["untied"] and attempts == s["fixes"]:
        return
    with state.change() as st:
        st["failed"] = failed
        st["hands"] = hands
        st["untied"] = untied
        st["fixes"] = attempts


def read() -> list:
    """Every open PR, fetched, with its areas. A draft pushed in the last 20 minutes is left alone: an agent is still on it."""
    now = datetime.datetime.now(datetime.timezone.utc)
    s = state.read()
    ours = {r.get("pushed") for r in s["replays"].values()}
    native_trains = {number for number, record in s["train_prs"].items()
                     if record.get("head") and record.get("source_heads")}
    raw = [p for p in world.open_prs()
           if p["headRefOid"] in ours or str(p["number"]) in native_trains or
           not (p["isDraft"] and (now - datetime.datetime.fromisoformat(p["updatedAt"].replace("Z", "+00:00"))).total_seconds() < RECENT)]
    world.fetch([p["number"] for p in raw])
    s = state.read()
    prs = []
    for p in raw:
        sha = world.head_of(p["number"])
        files = world.changed_files(sha)
        if not files:
            continue  # nothing to check or merge yet
        areas = plan.check_areas(world.areas(files), files, config.SKIP_AREAS)
        conflicting = plan.is_conflicting(p["mergeable"], p["number"] in s["conflicting"])
        prs.append(plan.Pr(p["number"], sha, areas, p["isDraft"], conflicting, world.has_merge_commits(sha), p["headRefName"], plan.touches_ci(files)))
    return prs


def shape(prs: list, up: dict, dry_run: bool) -> None:
    heads = {str(p.number): p.sha for p in prs}
    if not dry_run:
        with state.change() as st:
            # A branch that moved since it needed hands has been worked on; judge it afresh.
            st["hands"] = {n: h for n, h in st["hands"].items() if heads.get(n) == h["sha"]}
    s = state.read()
    main = world.main_sha()
    for pr in prs:
        if str(pr.number) in s["train_prs"] or pr.branch.startswith(rebases.TRAIN_BRANCH_PREFIX):
            continue
        parent = up.get(pr.number)
        base = world.head_of(parent) if parent else main
        if any(t["status"] == train.OPEN and t["base"] == main and
               [sha for number, sha in t["members"] if number == pr.number] == [pr.sha]
               for t in s["trains"].values()):
            continue
        if _frozen_train_member(s, pr.number, pr.sha, heads):
            continue
        if not (pr.conflicting or pr.has_merge_commits):
            continue
        if s["hands"].get(str(pr.number), {}).get("sha") == pr.sha:
            continue
        running = s["replays"].get(str(pr.number))
        if running:
            if running.get("pushed") == pr.sha and running.get("base") == base:
                continue
            if running.get("sha") == pr.sha and running.get("pid") and agents._alive(running["pid"]):
                continue
        if dry_run:
            say(f"would replay #{pr.number}")
            continue
        code = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([sys.executable, "-m", "nzfleet", "replay", str(pr.number)], cwd=code,
                                env=config.with_private_tmpdir(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        with state.change() as st:
            st["replays"][str(pr.number)] = {"sha": pr.sha, "pid": proc.pid}


def _frozen_train_member(s: dict, number: int, sha: str, heads: dict) -> bool:
    for train_pr, record in s["train_prs"].items():
        source_heads = record.get("source_heads", {})
        if source_heads.get(str(number)) != sha:
            continue
        for batch in s["batches"].values():
            if batch.get("status") != "running":
                continue
            train_head = record.get("head")
            pid = batch.get("pid")
            if heads.get(train_pr) != train_head or not pid or not agents._alive(pid):
                continue
            if [head for member, head in batch.get("members", []) if str(member) == train_pr] != [train_head]:
                continue
            if train_head and world.is_ancestor(sha, train_head):
                return True
    return False


def settle_replay(n: int, sha: str, parent, outcome: str, detail, base: str | None = None) -> None:
    """Records what a replay came to, and starts an agent for one that needs a person's judgement."""
    onto = f"#{parent}" if parent else "main"
    if outcome == "replayed":
        with state.change() as st:
            record = st["replays"].setdefault(str(n), {"sha": sha})
            record["sha"] = sha
            record["pushed"] = detail
            record.pop("pid", None)
            if base:
                record["base"] = base
        tell_event(f"#{n} replayed onto {onto}")
    elif outcome == "hands":
        # What a person has to read comes first; redrawn screenshots are noise beside it.
        detail = sorted(detail, key=lambda f: plan.generated_areas([f]) is not None)
        with state.change() as st:
            st["hands"][str(n)] = {"sha": sha, "files": detail, "parent": parent}
        shown = ", ".join(detail[:4]) + (f" and {len(detail) - 4} more" if len(detail) > 4 else "")
        tell_event(f"#{n} needs hands: {shown}")


def batches(prs: list, up: dict, dry_run: bool) -> None:
    s = state.read()
    running = {b["group"] for b in s["batches"].values() if b["status"] == "running" and agents._alive(b["pid"])}
    for bid, b in s["batches"].items():
        if b["status"] == "running" and not agents._alive(b["pid"]):
            if not dry_run:
                with state.change() as st:
                    st["batches"][bid]["status"] = "lost"
                    for n, sha in b["members"]:
                        st["untied"][str(n)] = owner.record_untied(st["untied"].get(str(n)), sha, f"batch {bid} stopped without a verdict")
            say(f"batch {bid} {'would be marked lost' if dry_run else 'stopped without a verdict; its members wait for the next one'}")
    s = state.read()
    busy = machine.busy()
    in_flight = {int(n) for b in s["batches"].values() if b["status"] == "running" and agents._alive(b["pid"])
                 for n, _ in b["members"]}
    waiting = []
    for pr in prs:
        if pr.number in in_flight:
            continue
        passed = s["passed"].get(str(pr.number))
        failed = s["failed"].get(str(pr.number), {}).get("sha")
        held = s["hands"].get(str(pr.number), {}).get("sha") == pr.sha or owner.stopped(s["untied"].get(str(pr.number)), pr.sha)
        earlier = s["batches"].get(passed["batch"], {}) if passed else {}
        checked = earlier.get("checked_areas", [] if "formal" in pr.areas else earlier.get("areas", []))
        if not held and plan.needs_check(pr, plan.Passed(**passed) if passed else None, failed, checked):
            waiting.append(pr)
    # The light lane takes no slot and ignores the load: its checks take minutes and use little.
    heavy = running - {"light"}
    slots = 0 if busy else BATCH_SLOTS - len(heavy)
    light = [p for p in waiting if plan.group_of(p.areas) == "light"]
    paused = running | holds.held_groups(s["holds"])
    lanes = plan.batches(light, up, paused, 1) + plan.batches([p for p in waiting if p not in light], up, paused, slots)
    for group, members, areas in lanes:
        heads = {p.number: p.sha for p in prs}
        if dry_run:
            say(f"would batch {group}: {' '.join(map(str, members))} over {','.join(areas)}")
            continue
        bid, new = batching.start_batch(members, areas, group, {n: heads[n] for n in members})
        say(f"batch {bid}: {' '.join(map(str, members))} over {','.join(areas)}" if new else f"batch {bid} already checks {' '.join(map(str, members))}")


def _batch_id(group: str, batches: dict) -> str:
    """A batch id no earlier batch holds: two started in one second would otherwise overwrite each other's record."""
    base, n = f"{group}-{int(time.time())}", 0
    while (bid := base if not n else f"{base}-{n}") in batches:
        n += 1
    return bid


def queue_sibling_rebases(numbers: list, by_number: dict) -> None:
    """Says which passed PRs were left out for conflicting with an earlier one, and queues a rebase for each."""
    with state.change() as st:
        fresh = [n for n in numbers if st["hands"].get(str(n), {}).get("sha") != by_number[n].sha]
        for n in fresh:
            st["hands"][str(n)] = {"sha": by_number[n].sha, "files": [], "parent": None, "sibling": True}
    for n in fresh:
        say(f"#{n} passed but conflicts with a pull request queued ahead of it; a rebase is queued")


def ready(prs: list, dry_run: bool) -> list:
    s = state.read()
    by_number = {p.number: p for p in prs}
    holding = {}
    for key, value in s["passed"].items():
        n = int(key)
        if n not in by_number or key in s["train_prs"]:  # a train is mailed once, by trainrun.mail_released_trains
            continue
        batch = s["batches"].get(value["batch"], {})
        checked = batch.get("checked_areas", [] if "formal" in by_number[n].areas else batch.get("areas", []))
        if plan.covered(by_number[n].areas, checked) and \
                plan.still_ready(by_number[n], plan.Passed(**value), world.merged_since(value["main"])):
            holding[n] = plan.Passed(**value)
    started = {bid: b["started"] for bid, b in s["batches"].items()}
    order = plan.queue(holding, started)
    conflicting = world.merges_cleanly(order)
    order = [n for n in order if n not in conflicting]
    if conflicting and not dry_run:
        queue_sibling_rebases(conflicting, by_number)
    if not dry_run and len(order) == 1:  # a member of a train is made ready when the train lands; ready starts a full ci run
        for n in order:
            if by_number[n].draft:
                world.mark_ready(n)
    return order


def record_train(order: list, heads: dict) -> str:
    """The id of the train that merges `order` on today's main, recording it with the tree it must produce."""
    if not order:
        return ""
    members = [(n, heads[n]) for n in order]
    base = world.main_sha()
    known = next((t for t in state.read()["trains"].values()
                  if t["status"] == train.OPEN and t["base"] == base and t["members"] == [list(m) for m in members]), None)
    if known:
        return known["id"]
    tree = world.verified_tree(members)
    if not tree:
        return ""
    made = train.make(base, members, tree)
    with state.change() as st:
        st["trains"] = train.with_train(st["trains"], made)
    return made["id"]


def tell(order: list, heads: dict, dry_run: bool) -> None:
    s = state.read()
    if dry_run:
        return say(f"queue: {plan.land_command(train.open_for(s['trains'], order)) or 'empty'}")
    train_id = record_train(order, heads)
    command = plan.land_command(train_id)
    body = ("Land the train. nzfleet keeps this current; it only holds PRs that passed a batch on the main they land on, "
            "and `land` refuses if main has moved since.\n\n" + (f"```\n{command}\n```" if command else "Nothing is ready."))
    number = world.set_issue(QUEUE_TITLE, body)
    if plan.grew(s["notified"], order) or (train_id and train_id not in s["notified_trains"]):
        world.comment(number, f"Ready to merge:\n\n```\n{command}\n```")
        tell_event(f"ready to merge: {' '.join(map(str, order))}")
    with state.change() as st:
        st["notified"] = order
        st["notified_trains"] = [train_id] if train_id else []
    owner_tick.ready_set(order, heads, train_id, tell_event)


def deploy(dry_run: bool) -> None:
    main = world.main_sha()
    recent = world.deploy_runs()
    if not dry_run:
        owner_tick.deploys_started(recent, main, tell_news)
    if state.read()["deploy_runs"] is None:
        latest = None
        for run in recent:
            if run["status"] != "completed" or run["conclusion"] not in ("success", "failure"):
                continue
            if not world.is_ancestor(run["headSha"], main):
                continue
            gate = world.deploy_jobs(run["databaseId"]).get(world.DEPLOY_GATE)
            if gate in ("success", "failure"):
                latest = run
                break
        if not dry_run:
            with state.change() as st:
                st["deploy_runs"] = [run["databaseId"] for run in recent if run is not latest]
        runs = [latest] if latest else []
    else:
        runs = reversed(recent)
    for run in runs:
        sha, run_id = run["headSha"], run["databaseId"]
        if run["status"] != "completed" or run["conclusion"] not in ("success", "failure"):
            continue
        if not world.is_ancestor(sha, main) or run_id in (state.read()["deploy_runs"] or []):
            continue
        jobs = world.deploy_jobs(run_id)
        if jobs.get(world.DEPLOY_GATE) not in ("success", "failure"):
            continue
        failed = [name for name, outcome in jobs.items() if outcome == "failure"]
        ok = run["conclusion"] == "success"
        outcome = f"failed in {', '.join(failed)}" if failed else "completed" if ok else "failed"
        what = f"deploy.yaml run https://github.com/{world.UPSTREAM}/actions/runs/{run_id} {outcome}"
        if dry_run:
            say(f"would report {sha[:8]}: {what}")
            continue
        with state.change() as st:
            st["deploys"][sha] = {"at": time.time(), "run_id": run_id, "ok": ok, "what": what}
            st["deploy_runs"] = (st["deploy_runs"] + [run_id])[-200:]
        tell_news(f"deployed {sha[:8]}: {what}" if ok else f"deploy of {sha[:8]} failed: {what}")
        issue = world.issue(QUEUE_TITLE)
        if issue:
            world.comment(issue["number"], f"{'Deployed' if ok else 'Deploy failed for'} `{sha[:8]}`: {what}")


def failed_jobs(previous_current: dict, conclusions: dict, run_id: int) -> dict:
    """{job: first error line} for every failed job of the run; a log is fetched only for a job not seen failing before."""
    known = previous_current.get("failed_jobs", {})
    return {job: known.get(job) or plan.first_error_line(world.job_log(run_id, job))
            for job, c in conclusions.items() if c in owner.FAILING}


def main_health(dry_run: bool) -> None:
    """A code failure on main is acted on when the same job fails in code on two completed main runs in a row.

    One red run is often the machine (a daemon restarted mid-job, a full disk) in ways the log does not name.
    """
    sha = world.main_sha()
    jobs = world.ci_jobs(sha)
    s = state.read()
    observed_at = time.time()
    previous_current = s.get("main_ci_current", {})
    same_run = previous_current.get("sha") == sha and previous_current.get("run_id") == (jobs[2] if jobs is not None else None)
    if jobs is None:
        current = {"sha": sha, "status": "unknown", "conclusion": "unknown", "run_id": None,
                   "first_observed_at": previous_current.get("first_observed_at", previous_current.get("at", observed_at)) if same_run and previous_current.get("status") == "unknown" and previous_current.get("conclusion") == "unknown" else observed_at,
                   "at": observed_at}
        if not dry_run:
            with state.change() as st:
                st["main_ci_current"] = current
        return
    status, conclusions, run_id = jobs
    failed = failed_jobs(previous_current if same_run else {}, conclusions, run_id)

    def observed(current: dict) -> dict:
        return {**current, "failed_jobs": failed} if failed else current

    if failed and not dry_run:
        owner_tick.failed_jobs(sha, run_id, failed, tell_event)
    if status in ("queued", "in_progress", "requested", "waiting", "pending"):
        same_observation = same_run and previous_current.get("status") == status and previous_current.get("conclusion") == "pending"
        current = {"sha": sha, "status": status, "conclusion": "pending", "run_id": run_id,
                   "first_observed_at": previous_current.get("first_observed_at", previous_current.get("at", observed_at)) if same_observation else observed_at,
                   "at": observed_at}
        if not dry_run:
            with state.change() as st:
                st["main_ci_current"] = observed(current)
        return
    if status != "completed":
        same_observation = same_run and previous_current.get("status") == "unknown" and previous_current.get("conclusion") == "unknown"
        current = {"sha": sha, "status": "unknown", "conclusion": "unknown", "run_id": run_id,
                   "first_observed_at": previous_current.get("first_observed_at", previous_current.get("at", observed_at)) if same_observation else observed_at,
                   "at": observed_at}
        if not dry_run:
            with state.change() as st:
                st["main_ci_current"] = observed(current)
        return
    failures = any(c in ("failure", "timed_out", "startup_failure", "action_required") for c in conclusions.values())
    cancelled = any(c == "cancelled" for c in conclusions.values())
    ok = bool(conclusions) and all(c in ("success", "skipped", "neutral") for c in conclusions.values())
    conclusion = "failure" if failures else "cancelled" if cancelled else "success" if ok else "unknown"
    current_status = "cancelled" if conclusion == "cancelled" else "completed"
    same_observation = same_run and previous_current.get("status") == current_status and previous_current.get("conclusion") == conclusion
    current = {"sha": sha, "status": current_status, "conclusion": conclusion, "run_id": run_id,
               "first_observed_at": previous_current.get("first_observed_at", previous_current.get("at", observed_at)) if same_observation else observed_at,
               "at": observed_at}
    if not dry_run:
        with state.change() as st:
            st["main_ci_current"] = observed(current)
    if conclusion not in ("success", "failure"):
        return
    if not dry_run:
        with state.change() as st:
            before = st["main_ci"]
            st["main_ci"] = {"sha": sha, "run_id": run_id, "ok": ok,
                             "since": before["since"] if before.get("ok") == ok else observed_at,
                             "completed_at": before.get("completed_at", observed_at)
                             if before.get("sha") == sha and before.get("run_id") == run_id else observed_at}
    if sha in s["main_reported"]:
        return
    code, golden_moved = {}, False
    for job in (j for j, c in conclusions.items() if c == "failure"):
        log = world.job_log(run_id, job)
        if plan.classify(log) == "code":
            code[job] = ", ".join(plan.failed_tests(log)[:5]) or "see the job"
            golden_moved = golden_moved or shotsfix.GOLDEN_MOVED in log
    previous = s.get("main_code_failures", {})
    repeated = {job: tests for job, tests in code.items() if job in previous}
    if dry_run:
        return
    with state.change() as st:
        st["main_reported"] = (st["main_reported"] + [sha])[-50:]
        st["main_code_failures"] = code
    if not code:
        return
    text = "; ".join(f"{job}: {tests}" for job, tests in code.items())
    tell_event(f"main {sha[:8]} is red in code: {text}" + (f"; a golden shot moved, `nzfleet shots fix {sha[:8]}` says why" if golden_moved else ""))
    if repeated and not dry_run:
        detail = "; ".join(f"{job}: {tests}" for job, tests in repeated.items())
        say(agents.start(f"main-{sha[:8]}", f"Main's CI has failed twice in a row in code ({sha}): {detail}. Find the "
                         f"cause and open a PR that fixes it at the root; a test that fails without the fix is required."
                         + (f" A golden screenshot moved: run `nzfleet shots fix {sha[:8]}` first, it names a canvas break or drift." if golden_moved else "")))


def comments(dry_run: bool) -> None:
    s = state.read()
    since = s["comments_since"] or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest = since
    for cid, number, body, url, at in world.owner_comments(since):
        if at <= since:
            continue
        newest = max(newest, at)
        task = (f"The owner commented on #{number} ({url}):\n\n{body}\n\nIf #{number} is a PR, address the comment by "
                f"pushing to the same branch and reply to the comment saying what changed. If it is an issue, do what "
                f"it asks in a new PR, or reply with a clear answer if no change is needed.")
        say(agents.start(f"comment-{cid}", task, dry_run=dry_run) or f"comment {cid} already handled")
    if not dry_run:
        with state.change() as st:
            st["comments_since"] = newest


def standings(s: dict, main_head: str | None = None) -> dict:
    """`plan.pr_standing` for every PR a queued item may be waiting on. Fetches those PRs' heads."""
    # A run recorded without its area, as `attach` once made, still gates the next item in that area.
    in_area = {t["key"] for t in s["todo"] if t.get("area")}
    numbers = sorted({a["pr"]["number"] for key, a in s.get("agents", {}).items()
                      if a.get("pr") and (a.get("area") or runs.base_key(key) in in_area)}
                     | holds.pending_prs(s["todo"]))
    if not numbers:
        return {}
    main_head = main_head or world.main_sha()
    world.fetch(numbers)
    out = {}
    merged = world.merged_prs()
    for n in numbers:
        head = world.head_of(n)
        passed = s["passed"].get(str(n))
        if passed and passed["sha"] != head and world.change_id(head, main_head) == world.change_id(passed["sha"], passed["main"]):
            head = passed["sha"]  # replayed or reshot, but the change that passed
        landed = n in merged or world.is_ancestor(head, main_head)
        closed = False
        if not landed:
            closed, comments = world.pr_comments(n)
            if closed:
                landed = landed_mod.landed_in(comments) is not None or world.change_on_main(head, main_head=main_head)
        out[n] = plan.pr_standing(head, passed, landed, closed)
    return out


def train_landed(dry_run: bool) -> set:
    """A merged train PR makes every member merged: each is recorded, closed on GitHub, and loses its queued rebase.
    Returns the members just closed, so the rest of the tick does not plan for them."""
    s = state.read()
    closed: set = set()
    for pr in landed_mod.newly_landed(s["train_prs"], world.merged_prs()):
        members = s["train_prs"][str(pr)]["members"]
        if dry_run:
            say(f"would settle {' '.join('#' + str(n) for n in members)} as landed in #{pr}")
            continue
        open_numbers = {p["number"] for p in world.open_prs()}
        retire_rebase_work(members)
        for n in members:
            if n in open_numbers:
                world.close_landed(n, landed_mod.comment(pr))
                closed.add(n)
        with state.change() as st:
            for n in members:
                st["merged_at"].setdefault(str(n), time.time())
            st["train_prs"][str(pr)]["landed"] = True
        tell_news(f"#{pr} landed; {' '.join('#' + str(n) for n in members)} closed as landed in it")
    return closed


def train_abandoned(dry_run: bool) -> None:
    """A train PR closed without merging releases its members, so they can join a train again."""
    s = state.read()
    open_prs = world.open_prs()
    if len(open_prs) >= world.OPEN_LIMIT:
        return
    for pr in landed_mod.abandoned(s["train_prs"], {p["number"] for p in open_prs}, world.merged_prs()):
        if dry_run:
            say(f"would release the members of #{pr}, closed without merging")
            continue
        with state.change() as st:
            st["train_prs"][str(pr)]["abandoned"] = True
        tell_news(f"#{pr} was closed without merging; its members are free for another train")


def work(dry_run: bool, main_head: str | None = None) -> None:
    """Starts the next rebase that needs hands, else the next queued task, when fewer than AGENT_SLOTS agents run and the machine has room."""
    s = state.read()
    agents_now = s.get("agents", {})
    running = sum(1 for a in agents_now.values() if a["status"] == "running")
    item = plan.next_work(s["todo"], agents_now, running, config.AGENT_SLOTS, machine.busy(), standings(s, main_head), s["holds"], s.get("area_heads", {}))
    if item and item.get("now"):
        return start_item(item, dry_run)
    hands = rebases.next_hands(s["hands"], agents_now, running, config.AGENT_SLOTS, machine.busy(), barred_rebases(s))
    if hands:
        n, task = hands
        said = agents.start(f"hands-{n}", task, dry_run=dry_run, pr=n)
        if f"hands-{n}" not in state.read()["refused"]:
            return tell_event(said or f"hands-{n} already started")
        if said:
            tell_event(said)
    standing = standings(s, main_head)
    if not dry_run:
        retire_closed_afters(standing)
    s = state.read()
    item = plan.next_work(s["todo"], agents_now, running, config.AGENT_SLOTS, machine.busy(), standing, s["holds"], s.get("area_heads", {}))
    if item:
        start_item(item, dry_run)


def barred_rebases(s: dict) -> set:
    """The PRs no hands rebase may start or resume for, using records when GitHub is unavailable."""
    try:
        open_prs = world.open_prs()
    except Exception:
        open_prs = []
    siblings = frozenset(int(n) for n, h in s.get("hands", {}).items() if h.get("sibling"))
    return rebases.barred(open_prs, s["train_prs"], task_queue.in_flight_members(s), s["holds"], siblings)


def start_item(item: dict, dry_run: bool) -> None:
    said = agents.start(item["key"], plan.stacked_task(item), item.get("model", "sonnet"), dry_run, item.get("area", ""),
                        pr=item["pr"], provider_override=item.get("provider"), pinned=bool(item.get("provider")))
    refused = state.read()["refused"].get(item["key"])
    if refused and not dry_run:
        with state.change() as st:
            retire_items(st, {item["pr"]: refused["reason"]})
            st["refused"].pop(item["key"], None)
    tell_event(said or f"{item['key']} already started")
    if item.get("skipped") and said and said.startswith("started"):
        tell_news(f"{item['key']} starts from main: #{item['skipped']}, which its area waited on, was closed without landing")


def shadow_fix(chooser, pending: dict, log, n: str, failure: dict, record, run_state: str) -> None:
    """Logs what Jev would decide for this red PR beside the rule, which is what runs, and judges the previous such choice."""
    if chooser is None:
        return
    slot, held = f"fix:{n}", pending.get(f"fix:{n}")
    if held:
        result = jev_points.fix_outcome(held["acted"], True, bool(record and record["told"]))
        if result and log.set_outcome(held["id"], result):
            pending.pop(slot)
    view = jev_points.fix_state(failure, record, run_state)
    rule = jev_points.fix_rule(view, ())
    decision = jev_points.consult(jev_points.FIX, rule, view, jev_points.FIX_OPTIONS, jev_points.FIX_INSTRUCTIONS, chooser, time.time(), log)
    if decision and slot not in pending:
        pending[slot] = {"id": decision, "acted": rule, "at": time.time(), "commits": 0}


def settle_fix_decisions(pending: dict, log, failed: dict) -> None:
    """A fix decision for a PR that is no longer red has its outcome."""
    for slot in [k for k in pending if k.startswith("fix:") and k[4:] not in failed]:
        if log.set_outcome(pending[slot]["id"], jev_points.fix_outcome(pending[slot]["acted"], False, False)):
            pending.pop(slot)


def fix_failures(dry_run: bool) -> None:
    """Starts one fix agent for each PR whose batch is red in its own code, and tells the owner when that attempt did not work."""
    if dry_run:
        return
    s = state.read()
    running = sum(1 for a in s["agents"].values() if a["status"] == "running")
    chooser, pending, log = jev_points.shadow_jev(), dict(s.get("jev_pending", {})), jev.Log()
    for n, failure in sorted(s["failed"].items(), key=lambda kv: int(kv[0])):
        if n in s["train_prs"]:
            continue  # a train's red areas are queued by trainrun.queue_red_fixes
        record = s["fixes"].get(n)
        run_state = runs.run_state(fixes.key(n), s["agents"])
        step = fixes.decide(failure, record, run_state)
        shadow_fix(chooser, pending, log, n, failure, record, run_state)
        if step == "start" and running < config.AGENT_SLOTS and not machine.busy():
            task = fixes.fix_task(int(n), world.branch_of(int(n)), failure)
            if agents.start(fixes.key(n), task, area="fix", pr=int(n)).startswith("started"):
                running += 1
                with state.change() as st:
                    st["fixes"][n] = {"sha": failure["sha"], "told": False}
                tell_event(f"#{n} failed its batch in its own code; started a fix agent")
        elif step == "tell":
            letter = fixes.failure_mail(int(n), failure, failure["sha"] != record["sha"])
            if mail.send(letter.subject, letter.body):
                with state.change() as st:
                    st["fixes"][n]["told"] = True
                tell_news(f"#{n} did not pass after its fix agent; emailed the owner")
    if chooser is not None:
        settle_fix_decisions(pending, log, s["failed"])
        with state.change() as st:
            st["jev_pending"] = {**{k: v for k, v in st.get("jev_pending", {}).items() if k.startswith("supervise:")}, **pending}


def owner_loop(dry_run: bool, main_head: str | None = None) -> None:
    """Tells the owner what stands in their way: a chain that waits on them, a batch that is red, a failure outside the code."""
    if dry_run:
        return
    s = state.read()
    now = time.time()
    standing = standings(s, main_head)
    owner_tick.retire_closed_pr_tasks(s, now, tell_event)
    s = state.read()
    decision_tasks = task_queue.decision_tasks(s["todo"], s.get("task_history", {}), s.get("area_heads", {}))
    waiting = {**owner.decisions(decision_tasks, s.get("agents", {}), standing), **owner_tick.stuck_decisions(s, now), **s["needs_person"]}
    owner_tick.person_decisions(waiting, tell_news)
    owner_tick.escalate_stuck(s, now, lambda body: world.comment(config.OWNER_ISSUE, body))
    trainrun.queue_red_fixes()
    trainrun.mail_released_trains(say)
    owner_tick.red_batches(time.time(), tell_news)
    owner_tick.untied_failures(tell_news)
    owner_tick.digest(now, lambda: supervise_tick.progress_text(supervise_tick.Host(), standings(state.read(), main_head)))


def guard_checkout(dry_run: bool) -> None:
    line = checkout.repair(dry_run)
    if line:
        say(line)
        tell_event(line)


def tidy_machine(dry_run: bool) -> None:
    """Every 15 minutes: the build outputs, containers and worktrees nothing running is using."""
    s = state.read()
    if not dry_run and not tidying.due(s["tidied"], time.time()):
        return
    host = janitor.Host(list(config.BUILD_ROOTS), config.WORK, config.REPO, tmp=config.tmp_root(), state=s,
                        caches=janitor.caches(config.HOME, config.WARM, config.SCCACHE, config.CACHE, config.WORK), jev_act=config.JEV_ACT)
    for line in janitor.run(host, dry_run):
        say(line)
    if not dry_run:
        with state.change() as st:
            st["tidied"] = time.time()


def supervise(dry_run: bool) -> None:
    """Every tick: sample the agents and stop burners. Once an hour: stop what runs away, unblock idle chains, mail what was missed, say how it is going."""
    if dry_run:
        return
    host = supervise_tick.Host()
    supervise_tick.watch_agents(host, tell_news)
    supervise_tick.stop_burners(host, tell_news)
    if supervise_tick.due(state.read()["supervised"], host.now()):
        supervise_tick.hourly(host, standings, tell_news, tell_event)


def tidy(dry_run: bool) -> None:
    tidy_machine(dry_run)
    if not dry_run:
        for key in agents.reap():
            say(f"agent {key} finished")
    if not dry_run:
        for line in agents.record_prs():
            say(f"agent {line}")
    for line in agents.resume(dry_run, barred=barred_rebases(state.read())):
        say(line)


EVENTS = os.path.join(state.HOME, "logs", "events.log")


def tell_event(line: str) -> None:
    """Events go to their own log, which is what a person or agent tails for news."""
    say(line)
    with open(EVENTS, "a") as f:
        f.write(f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%S} {line}\n")


def tell_news(line: str) -> None:
    """An event the owner reads in the daily digest as well as the log."""
    tell_event(line)
    owner_tick.news(line)


def watch() -> bool:
    """Reads GitHub's events since the last call and tells each one. True when something happened."""
    s = state.read()
    events, etag = world.events(s["events_etag"])
    fresh = sorted((e for e in events if int(e["id"]) > s["events_seen"]), key=lambda e: int(e["id"]))
    for e in fresh:
        line = plan.describe(e)
        if line:
            tell_event(line)
    with state.change() as st:
        st["events_etag"] = etag
        if fresh:
            st["events_seen"] = max(int(e["id"]) for e in fresh)
    return bool(fresh)


def conflicts(prs: list, dry_run: bool = False) -> None:
    now = sorted(p.number for p in prs if p.conflicting)
    before = state.read()["conflicting"]
    for n in sorted(set(now) - set(before)):
        if dry_run:
            say(f"would report conflict #{n} no longer merges onto main")
        else:
            tell_event(f"conflict #{n} no longer merges onto main")
    if not dry_run:
        with state.change() as st:
            st["conflicting"] = now


def merges(dry_run: bool) -> None:
    """Says which PRs main has merged since the last tick."""
    head = world.main_sha()
    seen = state.read()["main_seen"]
    if not seen or seen != head:
        if dry_run:
            say(f"would sync origin main from {world.UPSTREAM}")
            return
        try:
            world.sync_fork_main()
        except world.Failed as e:
            say(f"fork main sync failed: {e}")
            return
    if seen and seen != head:
        merged = world.merged_since(seen)
        with state.change() as st:
            for n in merged or []:
                st["merged_at"].setdefault(str(n), time.time())
            st["trains"] = train.after_outside_merge(st["trains"])
        tell_news(f"main moved to {head[:8]}" + (f" with {' '.join('#' + str(n) for n in merged)}" if merged else ""))
    if not dry_run:
        with state.change() as st:
            st["main_seen"] = head


def forget_main_broken(dry_run: bool) -> None:
    """A batch that stopped on main's own failure leaves the books once main is green again; its open PRs are checked afresh."""
    s = state.read()
    cleared = unbreak.clear(s["batches"], {pr["number"] for pr in world.open_prs()}, s["main_ci"], world.is_ancestor)
    if dry_run:
        for bid in cleared.drop:
            say(f"would forget main-broken batch {bid}")
        return
    if not cleared.drop:
        return
    with state.change() as st:
        for bid in cleared.drop:
            del st["batches"][bid]
        for n in cleared.release:
            st["untied"].pop(str(n), None)
    for bid in cleared.drop:
        say(f"forgot main-broken batch {bid}")


def run(dry_run: bool = False) -> None:
    """Each step is independent: one failing is logged and the rest still run."""
    try:
        config.refresh()
    except ValueError as error:
        return say(f"invalid configuration: {error}")
    try:
        prs = read()
    except world.Failed as e:
        return say(f"could not read GitHub or git: {e}")
    try:
        main_head = world.main_sha()
    except world.Failed as e:
        return say(f"could not pin main for this tick: {e}")
    if not dry_run:
        try:
            prune_stale(world.open_prs(), world.merged_prs())
        except world.Failed as e:
            say(f"could not reconcile stale PR records: {e}")
    if not dry_run:
        s = state.read()
        if any(s["agents"].get(item["key"], {}).get("status") in ("done", "retired") for item in s["todo"]):
            with state.change() as st:
                st["todo"], st["task_history"], st["area_heads"] = task_queue.compact(
                    st["todo"], st.get("agents", {}), st.get("task_history", {}), st.get("area_heads", {}))
    for name, step in (("checkout", lambda: guard_checkout(dry_run)), ("merges", lambda: merges(dry_run)), ("conflicts", lambda: conflicts(prs, dry_run))):
        try:
            step()
        except world.Failed as e:
            say(f"{name} failed: {e}")
    try:
        gone = train_landed(dry_run)
    except world.Failed as e:
        gone = set()
        say(f"train landed failed: {e}")
    prs = [p for p in prs if p.number not in gone]
    try:
        train_abandoned(dry_run)
    except world.Failed as e:
        say(f"train abandoned failed: {e}")
    up = plan.parents(prs, world.is_ancestor)
    order = []
    for name, step in (("shape", lambda: shape(prs, up, dry_run)),
                       ("batches", lambda: batches(prs, up, dry_run)),
                       ("ready", lambda: order.extend(ready(prs, dry_run))),
                       ("tell", lambda: tell(order, {p.number: p.sha for p in prs}, dry_run)),
                       ("deploy", lambda: deploy(dry_run)),
                       ("main", lambda: main_health(dry_run)),
                       ("unbreak", lambda: forget_main_broken(dry_run)),
                       ("fix", lambda: fix_failures(dry_run)),
                       ("owner", lambda: owner_loop(dry_run, main_head)),
                       ("comments", lambda: comments(dry_run)),
                       ("work", lambda: work(dry_run, main_head)),
                       ("journeys", lambda: journeys_run.tick(journeys_run.Host(), tell_event, dry_run)),
                       ("supervise", lambda: supervise(dry_run)),
                       ("tidy", lambda: tidy(dry_run)),
                       ("snapshot", lambda: snapshot_tick.write(dry_run))):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the others
            say(f"{name} failed: {type(e).__name__}: {e}")
