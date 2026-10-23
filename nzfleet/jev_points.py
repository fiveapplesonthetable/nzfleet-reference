"""The decision sites Jev shadows: what each one sends, what the rule would do, and how its outcome is judged afterwards."""

from __future__ import annotations

import datetime
import enum
from typing import Any, Mapping

from . import config, fixes, jev, progress, supervise


class Supervision(enum.Enum):
    WAIT = "wait"
    NUDGE = "nudge"
    KILL_AND_REQUEUE = "kill_and_requeue"
    KILL_AND_HOLD = "kill_and_hold"
    STOP = "stop"


class FixStep(enum.Enum):
    START_FIX = "start_fix"
    WAIT = "wait"
    TELL_OWNER = "tell_owner"
    DONE = "done"
    STOP = "stop"


SUPERVISE_STATE = frozenset({
    "verdict", "nudged_before", "attempt", "phase", "phase_minutes", "note_minutes", "commit_minutes", "change_minutes",
    "test_minutes", "cpu_moved_minutes", "cpu_busy_minutes", "waiting_on", "lock_holder", "batch_running"})
FIX_STATE = frozenset({"has_attempt", "told", "new_head", "fix_run", "failed_tests", "areas"})

SUPERVISE = jev.Point("supervise.step", Supervision, jev.Consequence.REVERSIBLE, SUPERVISE_STATE)
FIX = jev.Point("fixes.decide", FixStep, jev.Consequence.REVERSIBLE, FIX_STATE)

SUPERVISE_OPTIONS = [
    (Supervision.WAIT, "Wait: the agent is working or the wait is about to end"),
    (Supervision.NUDGE, "Nudge: resume the agent and ask where it is"),
    (Supervision.KILL_AND_REQUEUE, "Kill the agent and requeue it in its worktree"),
    (Supervision.KILL_AND_HOLD, "Kill the agent and ask the owner"),
]
FIX_OPTIONS = [
    (FixStep.START_FIX, "Start the one fix agent for this red pull request"),
    (FixStep.WAIT, "Wait for the fix agent that is running"),
    (FixStep.TELL_OWNER, "Tell the owner the fix attempt did not work"),
    (FixStep.DONE, "Nothing more to do: the owner has been told"),
]
SUPERVISE_INSTRUCTIONS = ("Choose what to do with a coding agent that has been running. A long wait on a lock held by another process "
                          "is not a reason to kill it; an idle process tree with nothing written is.")
FIX_INSTRUCTIONS = "Choose what to do about a pull request whose batch failed in its own code. It gets one fix agent, and then the owner is told."

NUDGE_JUDGED_AFTER = 20 * 60
WAIT_JUDGED_AFTER = progress.STALL_AFTER


def _minutes(now: float, then: float | None) -> int | None:
    return None if then is None else max(0, int((now - then) // 60))


def supervise_state(act: progress.Activity, sample: progress.Sample, started: float, now: float, nudged_before: bool, attempt: int,
                    phase: progress.Current | None, last_note: float | None, waits: Mapping[str, Any], batch_running: bool) -> dict:
    """What the rule and Jev both see about one running agent, in whole minutes and names, never paths or output."""
    return {
        "verdict": progress.verdict(act, sample, started, now).value, "nudged_before": nudged_before, "attempt": attempt,
        "phase": phase.phase if phase else None, "phase_minutes": _minutes(now, phase.since) if phase else None,
        "note_minutes": _minutes(now, last_note), "commit_minutes": _minutes(now, act.last_commit),
        "change_minutes": _minutes(now, act.last_change), "test_minutes": _minutes(now, act.last_test),
        "cpu_moved_minutes": _minutes(now, sample.alive_at), "cpu_busy_minutes": _minutes(now, sample.busy_at),
        "waiting_on": list(waits.get("waiting_on", [])), "lock_holder": waits.get("lock_holder", ""), "batch_running": batch_running,
    }


def supervise_rule(state: Mapping[str, Any], _values) -> str:
    """supervise.step, in the menu's words."""
    step = supervise.step(progress.Verdict(state["verdict"]), state["nudged_before"])
    return {supervise.Step.NONE: Supervision.WAIT, supervise.Step.NUDGE: Supervision.NUDGE,
            supervise.Step.RESUME: Supervision.KILL_AND_REQUEUE, supervise.Step.EMAIL: Supervision.KILL_AND_HOLD}[step].value


def fix_state(failure: Mapping[str, Any], record: Mapping[str, Any] | None, run_state: str) -> dict:
    return {
        "has_attempt": record is not None, "told": bool(record and record["told"]),
        "new_head": bool(record and failure["sha"] != record["sha"]), "fix_run": run_state,
        "failed_tests": len(failure.get("tests") or []), "areas": list(failure.get("areas") or []),
    }


def fix_rule(state: Mapping[str, Any], _values) -> str:
    """fixes.decide, in the menu's words."""
    record = {"told": state["told"], "sha": "head"} if state["has_attempt"] else None
    step = fixes.decide({"sha": "other" if state["new_head"] else "head"}, record, state["fix_run"])
    return {"start": FixStep.START_FIX, "wait": FixStep.WAIT, "tell": FixStep.TELL_OWNER, "done": FixStep.DONE}[step].value


def outcome(acted: str, progressed: bool, elapsed: float) -> str | None:
    """How a supervision choice turned out, or None while it is too early to say: a nudge or a restart is wrong when twenty minutes pass without a commit or an edit."""
    if acted in (Supervision.KILL_AND_HOLD.value, Supervision.STOP.value):
        return "unknown"
    if progressed:
        return "ok"
    limit = WAIT_JUDGED_AFTER if acted == Supervision.WAIT.value else NUDGE_JUDGED_AFTER
    return "wrong" if elapsed >= limit else None


def fix_outcome(acted: str, still_red: bool, told_owner: bool) -> str | None:
    """A fix started is right when the head stops being red and wrong when the owner had to be told."""
    if acted != FixStep.START_FIX.value:
        return "unknown"
    if not still_red:
        return "ok"
    return "wrong" if told_owner else None


def progressed(act: progress.Activity, since: float, commits_then: int) -> bool:
    return act.commits > commits_then or any(t is not None and t > since for t in (act.last_commit, act.last_change, act.last_test))


def register() -> None:
    jev.POINTS[SUPERVISE.name] = (SUPERVISE, jev.Rule(supervise_rule))
    jev.POINTS[FIX.name] = (FIX, jev.Rule(fix_rule))


def shadow_jev() -> jev.Decider | None:
    """Returns Jev when shadow logging or gated actions are enabled."""
    return jev.TypeSafe() if config.JEV_SHADOW or config.JEV_ACT else None


def consult(point: jev.Point, rule: str, state: Mapping[str, Any], options, instructions: str, chooser: jev.Decider | None,
            now: float, log: jev.Log | None = None, selective: frozenset[str] = frozenset()) -> str | None:
    """Logs what Jev would choose beside `rule`, which is what the caller does. Returns the decision id, or None when nothing was asked or logged."""
    if chooser is None:
        return None
    rule_decider = jev.Rule(lambda _s, _v: rule)
    decider = jev.Selective(rule_decider, chooser, selective) if selective else jev.Shadow(rule_decider, chooser)
    when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    try:
        return jev.decide(point, decider, state, options, instructions, log or jev.Log(), when).id
    except OSError:
        return None


register()
