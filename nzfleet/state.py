"""state.json: what nzfleet has decided, read and written under one exclusive lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os

from . import task_queue
from .config import HOME

os.makedirs(HOME, exist_ok=True)
PATH = os.path.join(HOME, "state.json")
LOCK = os.path.join(HOME, "state.lock")

EMPTY = {
    "passed": {},     # pr -> {sha, main, order, batch}
    "failed": {},     # pr -> {sha, why, tests, log}
    "batches": {},    # id -> {group, members, areas, main, started, pid, status, verdict}
    "starves": {},    # pid -> {pids}: burner process groups active until the command ends
    "notified": [],   # the merge list the owner was last told about
    "notified_trains": [],  # the train that list was told as
    "deploys": {},    # sha -> latest observed deploy.yaml outcome
    "deploy_runs": None,  # run ids already reported; None means the observer has not taken its first snapshot
    "fixes": {},      # pr -> {sha, told}: the one fix agent a red head got, the head it started on, and whether the owner was told it failed
    "hands": {},      # pr -> {sha, files}: a replay that did not apply cleanly
    "main_ci": {},    # {sha, ok, since, completed_at}: last completed run and the current outcome interval
    "main_ci_current": {},  # {sha, status, conclusion, run_id, first_observed_at, at}: latest run observation
    "failed_jobs_told": [],  # "<run>:<job>" for each failed main job the owner was emailed about
    "main_reported": [],  # main SHAs whose code failure has been reported
    "agents": {},     # key -> {status, pid, task, model, log, started, finished, summary}
    "agent_session": {},  # provider/model selected for new runs in this fleet session
    "comments_since": "",  # ISO time of the newest owner comment already handled
    "main_seen": "",  # main's head at the last tick, to say what merged since
    "events_etag": "",  # GitHub's ETag for the events feed, so an unchanged feed costs nothing
    "events_seen": 0,   # the newest event id already told
    "conflicting": [],  # PRs GitHub reported conflicting at the last tick
    "replays": {},    # pr -> {sha, pid}: a replay running in the background, since a redraw takes minutes
    "worktrees": {},  # slug -> {path, base, base_pr, port}: what `nzfleet start` made, so ports are never given twice
    "flakes": {},     # test -> {count, last_seen, runs}: failures that passed on rerun
    "holds": {},      # task key or batch group -> {why, since}: what must not start until released
    "retired": {},    # task key -> {why, at, task, held_for}: held work that was done or superseded, taken off the queue
    "stale_shots": {},  # main -> the commit main's own shots were redrawn at, when a refresh PR was opened and its number
    "untied": {},     # pr -> {sha, times, told, why}: failures the PR's code did not cause, on its current head
    "mailed": [],     # "<pr>@<sha>" for each ready PR the owner was already emailed about
    "person_told": {},  # task key -> the decision the owner was emailed about, while it stands
    "batches_told": [],  # batch ids whose red verdict was reported
    "deploy_started": [],  # deploy run ids already reported as started
    "digest": [],     # [time, line] events waiting for the next daily email
    "digest_sent": 0.0,  # when the digest last went out
    "tidied": 0.0,    # when the tidy pass last ran
    "notes": {},      # task key -> [{at, phase, line}]: what the agent said it was doing, newest last
    "samples": {},    # run key -> {cpu, at, alive_at, busy_at}: its process tree's CPU seconds, and when they last moved
    "jev_pending": {},  # key -> {id, acted, at, commits}: a shadowed decision whose outcome is not yet known
    "supervised": 0.0,  # when the hourly pass last ran
    "merged_at": {},  # pr -> when nzfleet saw main merge it
    "needs_person": {},  # key -> the sentence the owner is emailed once while it stands (a stalled agent, a disk that cannot be pruned)
    "refused": {},    # task key -> {reason, at}: a task not dispatched because its PR is closed or its branch or head is gone
    "escalated": {},  # task key -> when its last log lines were posted to the owner issue after the retry also failed
    "trains": {},     # id -> {id, base, members, tree, status, why}: a verified set of PRs, the main it was checked on, and the tree the merge must produce
    "code_moved": {},  # {at, from, to}: the last time the daemon moved its own code to main
    "code_refused": "",  # the nzfleet commit whose tests failed, so it is not tried again until a newer one is green
    "blocked": {},    # {reason, since, mailed}: why the daemon cannot start any agent, while that stands
    "train_prs": {},  # pr -> {branch, members, left_out, dropped, compiled, fixed_for, reviewed, landed, abandoned, mailing, mailed}: mailing prevents a second send after an interrupted notification
    "train_refused": {},  # {why, at}: why the last `nzfleet train` opened no pull request because its screenshots could not be made current
    "journeys": {},   # {seen, run: {sha, pid, started}, last: {sha, at, steps, issues}}: the main the phone journeys last looked at, and the run going
    "todo": [],       # [{key, task, model, after}], after being task keys or pr:<n>: work to start when there is capacity, in order
    "task_history": {},  # completed queue entries, kept out of dispatch scans
    "area_heads": {},  # last completed queued task in each area, for the next lane gate
}


def _read() -> dict:
    try:
        with open(PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    # Keys this code does not know are kept: a batch started by older code must not erase what newer code wrote.
    merged = {**json.loads(json.dumps(EMPTY)), **data}
    merged["todo"] = [task_queue.typed(item) for item in merged["todo"]]
    return merged


@contextlib.contextmanager
def change():
    """Yields the state; whatever the block leaves in it is written back atomically."""
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        yield data
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, PATH)


def read() -> dict:
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return _read()
