"""Whether each agent and each chain is making progress, as pure functions of what was observed."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum

from . import runs

IDLE_RATE = 0.05          # CPU seconds per second below which a process tree is idle
HUNG_AFTER = 30 * 60      # nothing written and not one tick of CPU
STALL_AFTER = 45 * 60     # nothing committed or written while the CPU is idle
PHASE_LONG = 2 * 3600
HOUR, DAY = 3600, 24 * 3600
NOTE_LIMIT = 200
NOTES_KEPT = 50


class Verdict(str, Enum):
    MOVING = "moving"
    STALLED = "STALLED"
    HUNG = "HUNG"


class Phase(str, Enum):
    READ = "read"
    WRITE = "write"
    TEST = "test"
    FIX = "fix"
    PUSH = "push"


@dataclass(frozen=True)
class Sample:
    """The CPU seconds a process tree had used at `at`, when it last used any more and when it last used them quickly."""
    cpu: float
    at: float
    alive_at: float
    busy_at: float


@dataclass(frozen=True)
class Activity:
    """What a worktree shows: commits and changed files so far and when each last moved (None when never)."""
    commits: int
    files: int
    last_commit: float | None
    last_change: float | None
    last_test: float | None


@dataclass(frozen=True)
class Note:
    phase: Phase
    line: str


@dataclass(frozen=True)
class Current:
    phase: str
    since: float
    line: str


@dataclass(frozen=True)
class AgentRow:
    key: str
    state: str  # the verdict while it runs, else how its last run ended
    since_commit: float | None
    since_change: float | None
    since_test: float | None
    commits: int
    files: int
    cpu_minutes: float
    phase: str
    phase_for: float | None
    note: str
    phase_long: bool


@dataclass(frozen=True)
class ChainRow:
    area: str
    steps_hour: int
    steps_day: int
    run_median: float | None
    batch_median: float | None
    ready_median: float | None
    owner_wait: float
    machine_wait: float
    head: str | None
    head_for: float | None


def take_sample(prev: Sample | None, cpu: float, now: float) -> Sample:
    if prev is None:
        return Sample(cpu, now, now, now)
    grew = cpu - prev.cpu
    elapsed = now - prev.at
    return Sample(cpu, now, now if grew > 0 else prev.alive_at,
                  now if elapsed > 0 and grew / elapsed >= IDLE_RATE else prev.busy_at)


def verdict(act: Activity, sample: Sample, started: float, now: float) -> Verdict:
    """HUNG when nothing was written and the CPU did not move at all; STALLED when nothing was written and the CPU was idle."""
    last_work = max(t for t in (act.last_commit, act.last_change, started) if t is not None)
    if now - last_work >= HUNG_AFTER and now - sample.alive_at >= HUNG_AFTER:
        return Verdict.HUNG
    if now - last_work >= STALL_AFTER and now - sample.busy_at >= STALL_AFTER:
        return Verdict.STALLED
    return Verdict.MOVING


def parse_note(text: str) -> Note | None:
    """`<phase>: <one line>`, or None when the phase is not one of the known ones or the line is empty or too long."""
    phase, _, line = text.partition(":")
    line = line.strip()
    if phase.strip() not in {p.value for p in Phase} or not line or len(line) > NOTE_LIMIT or "\n" in line:
        return None
    return Note(Phase(phase.strip()), line)


def with_note(notes: list, note: Note, now: float) -> list:
    """`notes` with one more at the end; the newest fifty are kept."""
    return [*notes, {"at": now, "phase": note.phase.value, "line": note.line}][-NOTES_KEPT:]


def current_phase(notes: list, now: float) -> Current | None:
    """The newest note's phase, from the first of the notes in a row that named it."""
    if not notes:
        return None
    since = notes[-1]["at"]
    for note in reversed(notes):
        if note["phase"] != notes[-1]["phase"]:
            break
        since = note["at"]
    return Current(notes[-1]["phase"], since, notes[-1]["line"])


def phase_is_long(current: Current | None, now: float) -> bool:
    return current is not None and now - current.since > PHASE_LONG


def _ago(then: float | None, now: float) -> float | None:
    return None if then is None else max(0.0, now - then)


def agent_row(key: str, status: str, act: Activity, sample: Sample, started: float, notes: list, now: float) -> AgentRow:
    """A row for a run in `status`: a running one is judged, one that ended shows how."""
    current = current_phase(notes, now)
    state = verdict(act, sample, started, now).value if status == "running" else status
    return AgentRow(key, state, _ago(act.last_commit, now), _ago(act.last_change, now),
                    _ago(act.last_test, now), act.commits, act.files, sample.cpu / 60,
                    current.phase if current else "", _ago(current.since, now) if current else None,
                    current.line if current else "", phase_is_long(current, now))


def _median(values: list) -> float | None:
    return statistics.median(values) if values else None


def chain_rows(todo: list, agents: dict, batches: dict, passed: dict, merged_at: dict, standing: dict, now: float) -> list:
    """One row per area: steps done, the median time of each kind of step, who the chain waited on, and its head.

    A step is an agent run, a batch, or the wait for the owner's merge, counted when it ends. Time waiting on the machine is
    the gap between a run ending and its batch starting; time waiting on the owner is from a batch passing to the merge.
    """
    out = []
    for area in dict.fromkeys(t["area"] for t in todo if t.get("area")):
        items = [t["key"] for t in todo if t.get("area") == area]
        steps, owner_wait, machine_wait, head, head_for = [], 0.0, 0.0, None, None
        merged_before = None
        for key in items:
            found = runs.latest(key, agents)
            pr = runs.recorded_pr(key, agents)
            ran_from = runs.lineage(key, agents)[0][1]["started"] if found else None
            ran_to = found[1].get("finished") if found and found[1].get("status") == "done" else None
            if ran_to is not None:
                steps.append((ran_to, "run", ran_to - ran_from))
            n = pr["number"] if pr else None
            record = batches.get(passed.get(str(n), {}).get("batch", ""), {}) if n else {}
            if record.get("finished"):
                steps.append((record["finished"], "batch", record["finished"] - record["started"]))
                machine_wait += max(0.0, record["started"] - ran_to) if ran_to is not None else 0.0
                merged = merged_at.get(str(n))
                if merged is not None:
                    steps.append((merged, "ready", merged - record["finished"]))
                    owner_wait += max(0.0, merged - record["finished"])
                elif standing.get(n) == "passed":
                    owner_wait += max(0.0, now - record["finished"])
            complete = found is not None and found[1].get("status") == "done" and (n is None or standing.get(n) == "merged")
            if not complete and head is None:
                head = key
                head_for = _ago(merged_before if merged_before is not None else ran_from, now)
            merged_before = merged_at.get(str(n)) if n is not None else merged_before
        by_kind = lambda kind: _median([d for _, k, d in steps if k == kind])
        out.append(ChainRow(area, sum(now - e < HOUR for e, _, _ in steps), sum(now - e < DAY for e, _, _ in steps),
                            by_kind("run"), by_kind("batch"), by_kind("ready"), owner_wait, machine_wait, head, head_for))
    return out


def _span(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def health_line(agent_rows: list, chains: list) -> str:
    """One line: each agent's verdict and each chain's steps in the last hour, so it is plain who moved."""
    agents = ", ".join(f"{r.key} {r.state}" + (f" ({r.phase})" if r.phase else "") for r in agent_rows) or "no agents running"
    stepped = ", ".join(f"{c.area} {c.steps_hour} steps in the last hour, {c.steps_day} today" for c in chains) or "no chains"
    return f"progress: {agents}; {stepped}"


def table(agent_rows: list, chains: list) -> str:
    lines = [f"{'AGENT':28}{'STATE':9}{'COMMIT':>9}{'CHANGE':>9}{'TEST':>9}{'COMMITS':>8}{'FILES':>6}{'CPU':>8}  PHASE"]
    for r in agent_rows:
        phase = f"{r.phase} {_span(r.phase_for)}: {r.note}" if r.phase else "no note yet"
        lines.append(f"{r.key:28}{r.state:9}{_span(r.since_commit):>9}{_span(r.since_change):>9}{_span(r.since_test):>9}"
                     f"{r.commits:>8}{r.files:>6}{r.cpu_minutes:>7.0f}m  {phase}")
        if r.phase_long:
            lines.append(f"{'':28}over 2 h in {r.phase}: {r.note}")
    lines.append(f"\n{'CHAIN':16}{'HOUR':>5}{'DAY':>5}{'RUN':>9}{'BATCH':>9}{'READY':>9}{'OWNER':>9}{'MACHINE':>9}  HEAD")
    for c in chains:
        head = f"{c.head} for {_span(c.head_for)}" if c.head else "none, all merged"
        lines.append(f"{c.area:16}{c.steps_hour:>5}{c.steps_day:>5}{_span(c.run_median):>9}{_span(c.batch_median):>9}"
                     f"{_span(c.ready_median):>9}{_span(c.owner_wait):>9}{_span(c.machine_wait):>9}  {head}")
    return "\n".join(lines)
