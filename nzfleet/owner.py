"""What the owner is told and what needs them, as functions of plain data."""

from __future__ import annotations

from dataclasses import dataclass

from . import plan

DAY = 24 * 3600
RECENT = 3600
TRIES = 2  # a check outside the code runs again once, then waits for a person
RED = ("red", "red-once", "gave-up", "counterexample", "main-broken")
UNTIED = ("infrastructure", "main-broken", "lost")
STARTING = ("queued", "waiting", "pending", "in_progress")


@dataclass(frozen=True)
class Mail:
    subject: str
    body: str


def first_paragraph(body: str) -> str:
    paragraph = (body or "").strip().split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def ready_keys(order: list, heads: dict, train_id: str) -> list:
    """A PR counts as told about for one head only, and a set for one train only: a new push that passes again, or a train verified on a new main, is news."""
    return [f"{n}@{heads[n]}" for n in order] + [f"train:{train_id}"]


def is_new(order: list, mailed: list, heads: dict, train_id: str) -> bool:
    return bool(set(ready_keys(order, heads, train_id)) - set(mailed))


def ready_mail(order: list, texts: dict, train_id: str) -> Mail:
    what = "\n\n".join(f"#{n} {texts[n][0]}\n{first_paragraph(texts[n][1])}".rstrip() for n in order)
    count = "1 pull request is" if len(order) == 1 else f"{len(order)} pull requests are"
    return Mail(f"nzela: {count} ready to merge",
                f"{what}\n\nLand them, in this order, as verified:\n\n{plan.land_command(train_id)}\n")


def record_untied(previous, sha: str, why: str) -> dict:
    """The record of a failure the PR did not cause: counted per head, so a new push starts again."""
    times = previous["times"] + 1 if previous and previous["sha"] == sha else 1
    return {"sha": sha, "times": times, "told": previous["told"] if previous and previous["sha"] == sha else 0, "why": why}


def stopped(record, sha: str) -> bool:
    """True when this head has failed outside its code often enough that it waits for a person."""
    return bool(record) and record["sha"] == sha and record["times"] >= TRIES


def untied_mail(n: int, record: dict) -> Mail:
    if record["times"] < TRIES:
        return Mail(f"nzela: #{n} failed outside its code, trying once more",
                    f"The check of #{n} failed in a way its change did not cause: {record['why']}\n\nIt runs again once.")
    return Mail(f"nzela: #{n} failed outside its code twice",
                f"The check of #{n} failed outside its code again: {record['why']}\n\nIt will not run again by itself. "
                f"Push to the branch or run `nzfleet batch {n}` when the cause is gone.")


def decisions(todo: list, agents: dict, standing: dict) -> dict:
    """For each queued item that waits on a person, the one sentence that says what they decide."""
    return {item["key"]: gate["why"] for item in todo
            if item["key"] not in agents and item.get("area")
            for gate in [plan.area_gate(item, todo, agents, standing)]
            if not gate["go"] and gate.get("person")}


def decision_mail(key: str, sentence: str) -> Mail:
    return Mail(f"nzela: {key} needs you", sentence)


FAILING = ("failure", "timed_out", "startup_failure", "action_required")


def failed_job_key(run_id: int, job: str) -> str:
    return f"{run_id}:{job}"


def failed_job_mail(sha: str, job: str, line: str, url: str) -> Mail:
    return Mail(f"nzela: main {sha[:8]} failed {job}",
                f"The {job} job failed on main {sha[:8]} while the rest of CI is still running.\n\n{line}\n\n{url}")


def red_lines(bid: str, batch: dict) -> list:
    """One sentence per failure a batch found: the area, the test, and the PR it was traced to when it was."""
    found = batch.get("blamed") or ([{"pr": None, "line": batch.get("line", ""), "tests": batch.get("tests", [])}]
                                    if batch["status"] in RED else [])
    return [f"batch {bid} is red in {', '.join(plan.failed_areas(f['line'], batch['areas']))}: "
            f"{', '.join(f['tests'][:3]) or 'no test named'}" + (f", from #{f['pr']}" if f["pr"] else "") for f in found]


def fresh_reds(batches: dict, told: list, now: float) -> dict:
    """Batches that found a failure within the last hour and were not reported, so an old record is never news."""
    return {bid: b for bid, b in batches.items()
            if red_lines(bid, b) and bid not in told and now - b.get("finished", 0) < RECENT}


def main_moved(last: str, now: str) -> bool:
    return bool(last) and bool(now) and last != now


def digest_due(sent: float, now: float, lines: list, stuck: list = ()) -> bool:
    return bool(lines or stuck) and now - sent >= DAY


def digest_mail(lines: list, stuck_lines: list = (), progress: str = "") -> Mail:
    """The day's events, then every stuck task and how long it has been stuck, then who moved."""
    parts = ["\n".join(f"{at}  {line}" for at, line in lines) or "No events."]
    if stuck_lines:
        parts.append("STUCK\n" + "\n".join(stuck_lines))
    if progress:
        parts.append("PROGRESS\n" + progress)
    return Mail("nzela: the day's events", "\n\n".join(parts) + "\n")
