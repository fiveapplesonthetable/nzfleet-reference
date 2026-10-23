"""Jev's shadow of what a batch does with a red area: why it failed, what to do about it, and whether each failed test is a flake.
The batch acts on the rule; every answer is logged beside it and judged by what the batch found next."""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import config, jev, plan

SAMPLES = 3
MAX_TESTS = 8
LOAD_HIGH = 36.0


class Cause(enum.Enum):
    DEFECT = "defect"
    FLAKE = "flake"
    MACHINE = "machine"
    STALE_BRANCH = "stale_branch"
    SEMANTIC_CONFLICT = "semantic_conflict"
    STOP = "stop"


class Action(enum.Enum):
    RERUN_AREA = "rerun_area"
    BISECT_NOW = "bisect_now"
    SEND_BACK = "send_back"
    DROP_MEMBER = "drop_member"
    MACHINE_FAULT = "machine_fault"
    STOP = "stop"


class Flakiness(enum.Enum):
    FLAKE = "flake"
    DEFECT = "defect"
    LOAD = "load"
    INFRA = "infra"
    STOP = "stop"


AREA_STATE = frozenset({"area", "rc", "tests", "tail", "lock_waits", "lock_holders", "load", "members", "rerun_allowed",
                        "infrastructure", "counterexample", "in_ledger"})
TEST_STATE = frozenset({"test", "area", "lines", "ledger_count", "load", "members", "other_areas_red", "infrastructure"})

CAUSE = jev.Point("batch.red_cause", Cause, jev.Consequence.REVERSIBLE, AREA_STATE, mode=jev.Mode.ACT)
ACTION = jev.Point("batch.red_action", Action, jev.Consequence.STOPS_WORK, AREA_STATE, mode=jev.Mode.ACT,
                   irreversible=frozenset({"drop_member", "send_back"}))
FLAKY_TEST = jev.Point("batch.test_flake", Flakiness, jev.Consequence.REVERSIBLE, TEST_STATE, mode=jev.Mode.ACT)

CAUSE_OPTIONS = [
    (Cause.DEFECT, "The failure is in the changed code and will fail again"),
    (Cause.FLAKE, "A test failed that passes on a clean rerun"),
    (Cause.MACHINE, "The machine failed: out of memory or disk, docker, or a lock held by another job"),
    (Cause.STALE_BRANCH, "A member is behind main and fails on something main already fixed"),
    (Cause.SEMANTIC_CONFLICT, "Each member passes alone and they fail together"),
]
ACTION_OPTIONS = [
    (Action.RERUN_AREA, "Run the red area again unchanged"),
    (Action.BISECT_NOW, "Bisect the members now"),
    (Action.SEND_BACK, "Send back the one member whose files the log names"),
    (Action.DROP_MEMBER, "Drop a member from the train"),
    (Action.MACHINE_FAULT, "Treat it as a machine fault and run it again later"),
]
TEST_OPTIONS = [
    (Flakiness.FLAKE, "The test is flaky and passes on a rerun"),
    (Flakiness.DEFECT, "The test fails because of the change"),
    (Flakiness.LOAD, "The test failed only because the machine was loaded"),
    (Flakiness.INFRA, "The test failed because of the environment, not the code"),
]
CAUSE_INSTRUCTIONS = "A batch area is red. Pick the cause from the log tail, the failed tests and the machine state."
ACTION_INSTRUCTIONS = "A batch area is red. Pick the next action from the log tail, the failed tests and the members."
TEST_INSTRUCTIONS = "One test failed in a batch. Pick whether it is flaky, a defect, load or infrastructure."


def _rule_cause(state: Mapping[str, Any], _values: Sequence[str]) -> str:
    """The batch reruns a train before it blames anyone, so a train failure counts as a flake until the rerun says otherwise."""
    if state["infrastructure"]:
        return Cause.MACHINE.value
    if state["members"] == 1 or state["counterexample"]:
        return Cause.DEFECT.value
    return Cause.FLAKE.value


def _rule_action(state: Mapping[str, Any], _values: Sequence[str]) -> str:
    if state["infrastructure"]:
        return Action.MACHINE_FAULT.value
    if state["members"] == 1:
        return Action.SEND_BACK.value
    return Action.RERUN_AREA.value if state["rerun_allowed"] and not state["counterexample"] else Action.STOP.value


def _rule_test(state: Mapping[str, Any], _values: Sequence[str]) -> str:
    if state["infrastructure"]:
        return Flakiness.INFRA.value
    return Flakiness.DEFECT.value if state["members"] == 1 else Flakiness.FLAKE.value


jev.POINTS.update({CAUSE.name: (CAUSE, jev.Rule(_rule_cause)), ACTION.name: (ACTION, jev.Rule(_rule_action)),
                   FLAKY_TEST.name: (FLAKY_TEST, jev.Rule(_rule_test))})


@dataclass(frozen=True)
class Facts:
    """What the batch found after the decision, judged against each choice. `blamed` is None until a member is named."""

    rerun_green: bool | None = None
    blamed: tuple[int, ...] | None = None
    main_broken: bool = False
    infrastructure: bool = False
    still_failing: frozenset[str] = frozenset()
    load_high: bool = False


def _verdict(known: bool | None, right: bool = True) -> str:
    return "unknown" if known is None else "ok" if known == right else "wrong"


def cause_outcome(choice: str, facts: Facts) -> str:
    """Whether a cause the batch can check turned out right: `unknown` where nothing it did bears on the choice."""
    if choice == Cause.FLAKE.value:
        return _verdict(facts.rerun_green)
    if choice == Cause.DEFECT.value:
        return "ok" if facts.rerun_green is False or facts.blamed else _verdict(facts.rerun_green, False)
    if choice == Cause.MACHINE.value:
        return "ok" if facts.infrastructure else "wrong" if facts.rerun_green is False else "unknown"
    if choice == Cause.SEMANTIC_CONFLICT.value:
        return "ok" if facts.blamed else "wrong" if facts.rerun_green else "unknown"
    if choice == Cause.STALE_BRANCH.value:
        return "wrong" if facts.rerun_green else "unknown"
    return "unknown"


def action_outcome(choice: str, facts: Facts) -> str:
    """Whether an action would have saved the batch work it set out to save."""
    if choice == Action.RERUN_AREA.value:
        return _verdict(facts.rerun_green)
    if choice == Action.BISECT_NOW.value:
        return "wrong" if facts.rerun_green else "ok" if facts.blamed or facts.main_broken else "unknown"
    if choice in (Action.SEND_BACK.value, Action.DROP_MEMBER.value):
        return "wrong" if facts.rerun_green or facts.main_broken else "ok" if facts.blamed else "unknown"
    if choice == Action.MACHINE_FAULT.value:
        return "ok" if facts.infrastructure or facts.rerun_green else "wrong" if facts.rerun_green is False else "unknown"
    return "unknown"


def flake_outcome(choice: str, test: str, facts: Facts) -> str:
    """Whether the rerun agreed with a classification of one test."""
    if facts.rerun_green is None:
        return "unknown"
    passed = test not in facts.still_failing
    if choice == Flakiness.FLAKE.value:
        return _verdict(passed)
    if choice == Flakiness.DEFECT.value:
        return _verdict(passed, False)
    if choice == Flakiness.LOAD.value:
        return "ok" if passed and facts.load_high else "wrong" if not passed else "unknown"
    if choice == Flakiness.INFRA.value:
        return "ok" if facts.infrastructure else "wrong" if not passed else "unknown"
    return "unknown"


@dataclass(frozen=True)
class Asked:
    """The decisions logged for one red area, kept so the batch can judge them when it knows more."""

    ids: tuple[tuple[str, str, Mapping[str, Any]], ...] = ()
    tests: Mapping[str, str] = field(default_factory=dict)
    action: str | None = None
    escalation: str | None = None


def area_state(area: str, result: Mapping[str, Any], batch: Mapping[str, Any]) -> dict:
    """The facts about one red area: the result's scrubbed tail and lock fields plus the batch's own."""
    log = result.get("log", "")
    return {"area": area, "rc": result["rc"], "tests": plan.failed_tests(log)[:MAX_TESTS], "tail": result.get("tail", []),
            "lock_waits": result.get("lock_waits", []), "lock_holders": result.get("lock_holders", []), "load": result.get("load"),
            "members": batch["members"], "rerun_allowed": batch["rerun_allowed"], "infrastructure": plan.classify(log) == "infrastructure",
            "counterexample": plan.counterexample(log), "in_ledger": [t for t in plan.failed_tests(log) if t in batch["ledger"]][:MAX_TESTS]}


def flake_state(test: str, area: str, result: Mapping[str, Any], batch: Mapping[str, Any], other_areas_red: int) -> dict:
    return {"test": test, "area": area, "lines": [l for l in plan.lines_about(result.get("log", ""), test).splitlines()],
            "ledger_count": batch["ledger"].get(test, {}).get("count", 0), "load": result.get("load"), "members": batch["members"],
            "other_areas_red": other_areas_red, "infrastructure": plan.classify(result.get("log", "")) == "infrastructure"}


class Triage:
    """Asks Jev about each red area and records it. The first Jev failure of a batch ends the asking for that batch, so a dead API costs one call."""

    def __init__(self, asker: jev.Decider, log: jev.Log, clock=lambda: datetime.datetime.now(datetime.timezone.utc)):
        self._asker, self._log, self._clock = asker, log, clock
        self._down = False

    def _ask(self, point: jev.Point, state, options, instructions, act_choices=frozenset()) -> tuple[str, str, Mapping[str, Any]] | None:
        if self._down:
            return None
        try:
            rule = jev.POINTS[point.name][1]
            decider = (jev.Selective(rule, self._asker, act_choices)
                       if config.JEV_ACT and act_choices else jev.Shadow(rule, self._asker))
            decision = jev.decide(point, decider, state, options, instructions, self._log, self._clock())
        except (jev.JevError, OSError, ValueError):
            self._down = True
            return None
        record = decision.record
        self._down = record["jev"] is None
        return decision.id, point.name, record

    def red_area(self, areas: Sequence[str], result: Mapping[str, Any], batch: Mapping[str, Any]) -> Asked:
        """Every red area gets one cause, `SAMPLES` actions and one classification per failed test."""
        ids, tests, action, escalation = [], {}, None, None
        for area in areas:
            state = area_state(area, result, batch)
            ids += [a for a in (self._ask(CAUSE, state, CAUSE_OPTIONS, CAUSE_INSTRUCTIONS) for _ in range(1)) if a]
            actable = frozenset({Action.RERUN_AREA.value, Action.BISECT_NOW.value}) if state["rerun_allowed"] and not state["counterexample"] else frozenset()
            for asked in (self._ask(ACTION, state, ACTION_OPTIONS, ACTION_INSTRUCTIONS, actable) for _ in range(SAMPLES)):
                if asked:
                    ids.append(asked)
                    if asked[2].get("acted") == "jev" and action is None:
                        action = asked[2].get("jev")
                    if asked[2].get("escalation") and escalation is None:
                        escalation = asked[2]["escalation"]
            for test in state["tests"]:
                asked = self._ask(FLAKY_TEST, flake_state(test, area, result, batch, len(areas) - 1), TEST_OPTIONS, TEST_INSTRUCTIONS)
                if asked:
                    ids.append(asked)
                    tests[asked[0]] = test
        return Asked(tuple(ids), tests, action, escalation)

    def settle(self, asked: Asked, facts: Facts) -> None:
        """Judges every logged choice, the rule's and Jev's, against what the batch found."""
        def judge(point: str, choice: str | None, decision_id: str) -> str:
            if choice is None:
                return "unknown"
            if point == FLAKY_TEST.name:
                return flake_outcome(choice, asked.tests[decision_id], facts)
            return (cause_outcome if point == CAUSE.name else action_outcome)(choice, facts)

        try:
            self._log.set_outcomes({i: (judge(p, r["rule"], i), judge(p, r["jev"], i)) for i, p, r in asked.ids})
        except OSError:
            self._down = True


class Off:
    """The batch's default: no asking, so a test never reaches the network."""

    def red_area(self, areas, result, batch) -> Asked:
        return Asked()

    def settle(self, asked, facts) -> None:
        return None


def live() -> Triage:
    """Jev through TypeSafe beside the rules, logged under the fleet home."""
    return Triage(jev.TypeSafe(), jev.Log())
