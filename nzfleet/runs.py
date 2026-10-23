"""A task's runs, as pure functions of the agents' records: which run is the latest, whether it finished, whether to resume it."""

from __future__ import annotations

import re
from dataclasses import dataclass

RESUME_LIMIT = 1  # one automatic retry; a second failure goes to the owner
BACKOFF = (120,)  # seconds to wait before the retry
ABANDONED = "abandoned"  # a run the supervisor will not resume again; a person decides
SHOWN_COMMITS, SHOWN_FILES, TAIL_LINES = 10, 30, 8

_SUFFIX = re.compile(r"~(\d+)$")
_NOISE = re.compile(r"^(Permission allow rule|Warning:)")
_REPORTED = re.compile(r"(?:PR|pull request) #?\d+|/pull/\d+|#\d{3,}\b|^PR: (?:#\d+|none)\s*$", re.I | re.M)
_RANK = {"attached": 3, "stated": 2, "guessed": 1}

CONTINUE = ("A previous run of this task ended without its final report. Continue in the worktree it left, do not start "
            "over and do not make a new one. Push what is done, then `nzfleet prove`, `nzfleet pr`, and write the "
            "summary. Wait in the foreground, never leave anything running.")
NUDGE = ("The previous run of this task was stopped because nothing in its worktree changed for 45 minutes. Say where you "
         "are and what is blocking with `nzfleet note`, then carry on in the same worktree: do not start over.")


@dataclass(frozen=True)
class Stopped:
    """Where a run left its worktree: its path, the commits above its base and the uncommitted changes."""
    path: str
    commits: tuple
    changes: tuple


@dataclass(frozen=True)
class Stuck:
    """A task whose runs are used up without a report."""
    key: str
    runs: int
    since: float
    tail: tuple
    worktree: str


def base_key(key: str) -> str:
    """The task a run belongs to: `a~3` is the third run of `a`."""
    return _SUFFIX.sub("", key)


def attempt(key: str) -> int:
    found = _SUFFIX.search(key)
    return int(found.group(1)) if found else 1


def dispatch_refusal(key: str, holds: dict, slots: int) -> str:
    """Why an agent task cannot start under the fleet's current ownership and slot rules."""
    base = base_key(key)
    if base in holds:
        return f"{base} is held: {holds[base]['why']}"
    return "automatic agent starts are paused because NZFLEET_AGENT_SLOTS is 0" if slots <= 0 else ""


def run_key(base: str, n: int) -> str:
    return base if n == 1 else f"{base}~{n}"


def lineage(base: str, agents: dict) -> list:
    """The task's runs as (key, record), first to latest."""
    out, n = [], 1
    while run_key(base, n) in agents:
        out.append((run_key(base, n), agents[run_key(base, n)]))
        n += 1
    return out


def latest(base: str, agents: dict):
    """The task's newest run as (key, record), or None before it has started."""
    runs = lineage(base, agents)
    return runs[-1] if runs else None


def run_state(key: str, agents: dict) -> str:
    """`queued`, `running`, `done`, `resuming` (an unfinished run with resumptions left) or `stuck` for a task.

    A run a person stopped, or one the supervisor gave up resuming, is `stuck` until a person acts.
    """
    found = latest(key, agents)
    if found is None:
        return "queued"
    run_id, agent = found
    if agent.get("status") == "unfinished" and attempt(run_id) <= RESUME_LIMIT:
        return "resuming"
    return {"running": "running", "done": "done"}.get(agent.get("status"), "stuck")


def _best_pr(key: str, agents: dict):
    """The most trustworthy PR recorded on any run of the task: attached, else stated, else guessed; the newest of equals."""
    found = [a["pr"] for _, a in reversed(lineage(key, agents)) if a.get("pr")]
    return max(found, key=lambda pr: _RANK.get(pr.get("how", "guessed"), 1), default=None)


def recorded_pr(key: str, agents: dict):
    return _best_pr(key, agents)


def pr_problem(key: str, agents: dict):
    """Why the PR recorded for a finished task cannot be built on, as (sentence, a person must decide), or None.

    A run that finished and was looked for but has no PR changed nothing, so nothing is waited on; one that stated
    `PR: none` having committed work is a person's call.
    """
    runs = [a for _, a in lineage(key, agents)]
    pr = _best_pr(key, agents)
    if pr and pr.get("how", "guessed") == "guessed":
        return (f"#{pr['number']} for {key} was guessed from logs, not stated; check it, "
                f"then `nzfleet attach {key} {pr['number']}`", True)
    if pr:
        return None
    if any(run.get("stated_no_pr") for run in runs) and not any(run.get("no_change") for run in runs):
        return f"{key} stated `PR: none`; a person decides (nzfleet attach {key} <pr>)", True
    if any("pr" in run for run in runs):
        return None
    return f"looking for the PR of {key}", False


def unfinished(log: str) -> bool:
    """A run whose log has no final report: nothing after the tool's warnings, or no PR line or PR number in its last lines."""
    lines = [line for line in log.splitlines() if line.strip() and not _NOISE.match(line)]
    return not _REPORTED.search("\n".join(lines[-25:]))


def needs_resume(agent: dict, agents: dict, key: str, now: float) -> bool:
    """An unfinished run is retried up to RESUME_LIMIT times after the backoff; a nudge goes at once."""
    n = attempt(key)
    if agent.get("status") != "unfinished" or n > RESUME_LIMIT or run_key(base_key(key), n + 1) in agents:
        return False
    return bool(agent.get("nudge")) or now - agent.get("finished", 0) >= BACKOFF[n - 1]


def resume_task(task: str, where, nudge: bool) -> str:
    """The brief for the next run: what to do, where the last one stopped, then the original task (empty when a session carries it)."""
    lead = NUDGE if nudge else CONTINUE
    if where is None:
        return f"{lead}\n\n{task}".strip()
    commits = list(where.commits[:SHOWN_COMMITS]) + ([f"and {len(where.commits) - SHOWN_COMMITS} more"] if len(where.commits) > SHOWN_COMMITS else [])
    changes = list(where.changes[:SHOWN_FILES]) + ([f"and {len(where.changes) - SHOWN_FILES} more"] if len(where.changes) > SHOWN_FILES else [])
    report = "\n".join([f"Its worktree is {where.path}.",
                        "git log against its base:", *(f"  {line}" for line in commits or ["(no commits)"]),
                        "git status:", *(f"  {line}" for line in changes or ["(clean)"])])
    return f"{lead}\n\n{report}\n\n{task}".strip()


def stuck_runs(agents: dict, now: float) -> list:
    """Every task whose latest run was given up on, or ended unfinished with no resumptions left, oldest first."""
    out = []
    for base in dict.fromkeys(base_key(k) for k in agents):
        key, agent = latest(base, agents) or (None, None)
        if agent is None or not (agent.get("status") == ABANDONED
                                 or agent.get("status") == "unfinished" and attempt(key) > RESUME_LIMIT):
            continue
        tail = tuple(line for line in agent.get("summary", "").splitlines() if line.strip())[-TAIL_LINES:]
        worktree = next((a["worktree"] for _, a in reversed(lineage(base, agents)) if a.get("worktree")), "")
        out.append(Stuck(base, attempt(key), agent.get("finished", now), tail, worktree))
    return sorted(out, key=lambda s: s.since)


def stuck_sentence(stuck: Stuck, where) -> str:
    """What the owner is told: the worktree, the last lines of the log and what the task needs. It does not change while the task stays stuck."""
    work = f"{len(where.commits)} commits and {len(where.changes)} changed files are in it" if where else "its worktree is unknown"
    needs = (f"someone to finish or push what is there, then `nzfleet attach {stuck.key} <pr>`"
             if where and (where.commits or where.changes) else
             "someone to queue the task again with `nzfleet todo`, or drop it; nothing was written")
    log = "\n".join(f"  {line}" for line in stuck.tail) or "  (its log is empty)"
    return (f"{stuck.key} ended {stuck.runs} times without its final report and will not be resumed again.\n"
            f"Worktree: {stuck.worktree or 'unknown'} ({work}).\nLast lines of its log:\n{log}\nIt needs: {needs}")


def stuck_digest_lines(stuck: list, now: float) -> list:
    return [f"{s.key} has been stuck for {_span(now - s.since)} after {s.runs} runs (worktree {s.worktree or 'unknown'})" for s in stuck]


def _span(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
