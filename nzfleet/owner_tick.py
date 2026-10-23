"""The owner loop's steps: tell the owner what is ready, what needs them and what happened. Each takes `tell`, which logs an event."""

from __future__ import annotations

import datetime

from . import agents, dispatch, mail, owner, runs, state, world


def _sent(letter: owner.Mail) -> bool:
    return mail.send(letter.subject, letter.body)


def news(line: str) -> None:
    """Keeps an event for the daily digest."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    with state.change() as s:
        s["digest"].append([stamp, line])


def ready_set(order: list, heads: dict, train_id: str, tell) -> None:
    """Emails the owner once for each ready set that holds a PR, or a train, they were not told about."""
    if not train_id or not owner.is_new(order, state.read()["mailed"], heads, train_id):
        return
    if _sent(owner.ready_mail(order, {n: world.pr_text(n) for n in order}, train_id)):
        with state.change() as s:
            s["mailed"] = (s["mailed"] + owner.ready_keys(order, heads, train_id))[-500:]
        tell(f"emailed the owner: {' '.join('#' + str(n) for n in order)} ready to merge")


def person_decisions(decisions: dict, tell) -> None:
    """Emails each decision a chain waits on, once while it stands."""
    told = state.read()["person_told"]
    fresh = {key: why for key, why in decisions.items() if told.get(key) != why}
    for key, why in fresh.items():
        tell(f"{key} needs a person: {why}")
        if _sent(owner.decision_mail(key, why)):
            told[key] = why
    with state.change() as s:
        s["person_told"] = {key: why for key, why in {**s["person_told"], **told}.items() if key in decisions}


def untied_failures(tell) -> None:
    """Says what a failure outside the code came to: retried once, or waiting for a person."""
    for n, record in sorted(state.read()["untied"].items()):
        if record["told"] >= record["times"]:
            continue
        tell(f"#{n} failed outside its code ({record['why']}); " +
             ("trying once more" if record["times"] < owner.TRIES else "not trying again, a person decides"))
        if _sent(owner.untied_mail(int(n), record)):
            with state.change() as s:
                s["untied"][n]["told"] = record["times"]


def failed_jobs(sha: str, run_id: int, failed: dict, tell) -> None:
    """Emails each failed job of a main run once, the first tick that sees it, even while the run goes on."""
    told = state.read()["failed_jobs_told"]
    for job, line in sorted(failed.items()):
        key = owner.failed_job_key(run_id, job)
        if key in told:
            continue
        tell(f"main {sha[:8]}: {job} failed: {line}")
        if _sent(owner.failed_job_mail(sha, job, line, f"https://github.com/{world.UPSTREAM}/actions/runs/{run_id}")):
            with state.change() as s:
                s["failed_jobs_told"] = (s["failed_jobs_told"] + [key])[-200:]


def red_batches(now: float, tell) -> None:
    """Reports each batch that ended red once."""
    s = state.read()
    fresh = owner.fresh_reds(s["batches"], s["batches_told"], now)
    for bid, b in sorted(fresh.items(), key=lambda kv: kv[1]["started"]):
        for line in owner.red_lines(bid, b):
            tell(line)
    if fresh:
        with state.change() as st:
            st["batches_told"] = (st["batches_told"] + sorted(fresh))[-200:]


def deploys_started(runs: list, main: str, tell) -> None:
    """Reports a deploy.yaml run for a commit on main once, when it starts."""
    seen = state.read()["deploy_started"]
    for run in reversed(runs):
        if run["status"] not in owner.STARTING or run["databaseId"] in seen or not world.is_ancestor(run["headSha"], main):
            continue
        if world.deploy_jobs(run["databaseId"]).get(world.DEPLOY_GATE) in ("skipped", "failure", "cancelled"):
            continue
        tell(f"deploy of {run['headSha'][:8]} started: https://github.com/{world.UPSTREAM}/actions/runs/{run['databaseId']}")
        with state.change() as s:
            s["deploy_started"] = (s["deploy_started"] + [run["databaseId"]])[-200:]


def stuck_decisions(s: dict, now: float) -> dict:
    """For each task that used up its resumptions, the sentence that tells the owner where it is and what it needs."""
    return {f"stuck-{stuck.key}": runs.stuck_sentence(stuck, agents.where_it_stopped(stuck.worktree, s))
            for stuck in runs.stuck_runs(s.get("agents", {}), now)}


def retire_closed_pr_tasks(s: dict, now: float, tell) -> None:
    """Retires stuck tasks whose recorded pull request is no longer open."""
    stuck_runs = runs.stuck_runs(s.get("agents", {}), now)
    candidates = [(stuck, _task_pr(stuck.key, s["agents"])) for stuck in stuck_runs]
    candidates = [(stuck, pr) for stuck, pr in candidates if pr]
    if not candidates:
        return
    open_numbers = {int(pr["number"]) for pr in world.open_prs()}
    for stuck, pr in candidates:
        if int(pr["number"]) in open_numbers:
            continue
        with state.change() as st:
            _, latest = runs.latest(stuck.key, st["agents"])
            latest["status"] = "retired"
        tell(f"retired stuck task {stuck.key}: PR #{pr['number']} is no longer open")


def _task_pr(key: str, agent_runs: dict):
    """The pull request a task belongs to or produced, if known."""
    for _, agent in reversed(runs.lineage(key, agent_runs)):
        if agent.get("for_pr"):
            return {"number": agent["for_pr"]}
    if key.startswith("hands-") and key.removeprefix("hands-").isdigit():
        return {"number": int(key.removeprefix("hands-"))}
    return runs.recorded_pr(key, agent_runs)


def escalate_stuck(s: dict, now: float, post) -> None:
    """Posts the last log lines of each task whose retry also failed to the owner issue, once, and records it."""
    for stuck in runs.stuck_runs(s.get("agents", {}), now):
        if stuck.key in s["escalated"]:
            continue
        _, last = runs.latest(stuck.key, s["agents"])
        try:
            log = open(last["log"], errors="replace").read()
        except OSError:
            log = ""
        post(dispatch.escalation(stuck.key, stuck.runs, log))
        with state.change() as st:
            st["escalated"][stuck.key] = now


def digest(now: float, progress=lambda: "") -> None:
    """Emails the day's events, the stuck tasks and the progress table once a day, if there is news or something is stuck."""
    s = state.read()
    if not s["digest_sent"]:
        with state.change() as st:
            st["digest_sent"] = now  # the first digest comes a day after nzfleet started telling
        return
    stuck = runs.stuck_digest_lines(runs.stuck_runs(s.get("agents", {}), now), now)
    if owner.digest_due(s["digest_sent"], now, s["digest"], stuck) and _sent(owner.digest_mail(s["digest"], stuck, progress())):
        with state.change() as st:
            st["digest"], st["digest_sent"] = [], now
