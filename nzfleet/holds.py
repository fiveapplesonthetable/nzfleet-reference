"""Holds and `--after` conditions: decisions about what may start, as pure functions of plain data."""

from __future__ import annotations

import re

GROUPS = ("android", "ci", "console", "core", "light", "wide")

_PR = re.compile(r"^pr:(\d+)$")


def pr_waited_on(entry: str):
    """The PR number an `--after` entry names with `pr:<n>`, or None when it names a task key."""
    found = _PR.match(entry)
    return int(found.group(1)) if found else None


def after_problem(entry: str) -> str:
    """Empty when `--after` can use this entry, else the sentence saying what is wrong with it."""
    if entry.startswith("pr:") and pr_waited_on(entry) is None:
        return f"'{entry}' is not a pull request: write pr:<number>, e.g. pr:954"
    return ""


def after_met(after: list, done: set, standing: dict) -> bool:
    """Every task finished and every `pr:<n>` merged. `standing` maps a PR number to its `plan.pr_standing`."""
    def met(entry: str) -> bool:
        n = pr_waited_on(entry)
        return standing.get(n) == "merged" if n is not None else entry in done

    return all(met(e) for e in after)


def closed_after(after: list, standing: dict):
    """The first `pr:<n>` in `--after` whose PR was closed without landing, or None; the task can never start."""
    return next((n for e in after if (n := pr_waited_on(e)) is not None and standing.get(n) == "closed"), None)


def pending_prs(todo: list) -> set:
    """The PRs that queued tasks wait on with `pr:<n>`, so their standing is looked up."""
    return {n for t in todo for e in t.get("after", []) if (n := pr_waited_on(e)) is not None}


def may_start(item: dict, done: set, standing: dict, holds: dict) -> bool:
    """A task starts when it is not held and what it is after has happened."""
    return item["key"] not in holds and after_met(item.get("after", []), done, standing)


_HANDS = re.compile(r"^hands-\d+(~\d+)?$")


def hold_problem(target: str, why: str, todo: list, holds: dict, agents: dict) -> str:
    """Empty when `target` can be held, else what to fix. A typo must not create a hold that holds nothing."""
    if not why.strip():
        return "say why with --why, so whoever reads status knows when to release it"
    if target in holds:
        return f"{target} is already held: {holds[target]['why']}"
    running = {key for key, a in agents.items() if a.get("status") == "running"}
    if target not in GROUPS and target not in {t["key"] for t in todo} | running and not _HANDS.match(target):
        return f"{target} is neither a queued task, a running agent, a hands-<pr> rebase nor a batch group ({', '.join(GROUPS)})"
    return ""


def release_problem(target: str, holds: dict) -> str:
    """Empty when `target` is held, else the sentence saying it is not."""
    return "" if target in holds else f"{target} is not held"


def with_hold(holds: dict, target: str, why: str, now: float) -> dict:
    """`holds` with `target` held for `why` since `now`."""
    return {**holds, target: {"why": why.strip(), "since": now}}


def without_hold(holds: dict, target: str) -> dict:
    """`holds` with `target` released."""
    return {k: v for k, v in holds.items() if k != target}


def held_groups(holds: dict) -> set:
    """The batch groups whose batches must not start."""
    return {k for k in holds if k in GROUPS}


def hold_lines(holds: dict, now: float) -> list:
    """One line per hold, oldest first: what is held, for how long, and why."""
    ordered = sorted(holds.items(), key=lambda kv: kv[1]["since"])
    return [f"{k:24} held {max(0, int((now - h['since']) // 60))} min: {h['why']}" for k, h in ordered]


STALE_HOLD = 24 * 3600


def retire_problem(key: str, why: str, todo: list, agents: dict) -> str:
    """Empty when `key` can be retired, else what to fix. A running task is stopped with `nzfleet kill`, not retired."""
    if not why.strip():
        return "say why with --why, so the retired list records what made the work unnecessary"
    if key not in {t["key"] for t in todo}:
        return f"{key} is not a queued task"
    if agents.get(key, {}).get("status") == "running":
        return f"{key} is running; stop it with nzfleet kill first"
    return ""


def retire(todo: list, holds: dict, retired: dict, key: str, why: str, now: float) -> tuple:
    """(todo, holds, retired) with `key` gone from the queue and its hold, recorded in `retired`, and no task still waiting on it."""
    item = next(t for t in todo if t["key"] == key)
    record = {"why": why.strip(), "at": now, "task": item["task"], "held_for": holds[key]["why"] if key in holds else ""}
    rest = [{**t, "after": [e for e in t.get("after", []) if e != key]} if key in t.get("after", []) else t for t in todo if t["key"] != key]
    return rest, without_hold(holds, key), {**retired, key: record}


def retired_lines(retired: dict) -> list:
    """One line per retired task, newest first."""
    ordered = sorted(retired.items(), key=lambda kv: -kv[1]["at"])
    return [f"{k:28} retired: {r['why']}" for k, r in ordered]


def stale_holds(holds: dict, todo: list, now: float) -> list:
    """Keys of queued tasks held for over a day, oldest first: each needs a decision to release or retire."""
    queued = {t["key"] for t in todo}
    return [k for k, h in sorted(holds.items(), key=lambda kv: kv[1]["since"]) if k in queued and now - h["since"] > STALE_HOLD]
