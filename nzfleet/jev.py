"""A cheap, logged choice between actions the code already knows how to execute, and the commands that replay and summarise those choices."""

from __future__ import annotations

import contextlib
import datetime
import enum
import fcntl
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import config, event_log, redact

STOP = "stop"
STOP_LABEL = "blocked, or nothing listed fits"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL_PREFIX = "jev-"
DEFAULT_KEY_FILE = "/srv/fleet/secrets/jev-api-key"
ATTEMPTS = 3


class JevError(Exception):
    """A failure whose message is safe to log: it never carries the key, a header or a URL."""


class Mode(enum.Enum):
    """ADVISE logs Jev beside the rule that acts; ACT lets Jev's choice through when it clears the floor."""

    ADVISE = "advise"
    ACT = "act"


class Consequence(enum.Enum):
    """What a wrong choice costs, which sets the confidence a choice needs before it is acted on."""

    REVERSIBLE = 0.6
    STOPS_WORK = 0.8


@dataclass(frozen=True)
class Point:
    """One decision site: the menu it may choose from, how much a wrong choice costs, and which state fields may leave the machine."""

    name: str
    menu: type[enum.Enum]
    consequence: Consequence
    allowed_state: frozenset[str]
    mode: Mode = Mode.ADVISE
    irreversible: frozenset[str] = frozenset()

    def __post_init__(self):
        values = {m.value for m in self.menu}
        if STOP not in values:
            raise ValueError(f"{self.name}: the menu has no {STOP!r} member")
        if not self.irreversible <= values:
            raise ValueError(f"{self.name}: irreversible names a value that is not on the menu")

    @property
    def floor(self) -> float:
        return self.consequence.value


@dataclass(frozen=True)
class Menu:
    """The options of one call, as (value, label) pairs with unique labels and a stop."""

    items: tuple[tuple[str, str], ...]

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.items)


def build_menu(point: Point, options: Sequence[tuple[enum.Enum, str]]) -> Menu:
    """Options in order, a repeated label or member dropped (the first stays), and stop added when absent."""
    items, labels = [], set()
    for member, label in options:
        if not isinstance(member, point.menu):
            raise ValueError(f"{point.name}: {member!r} is not a member of {point.menu.__name__}")
        if label in labels or member.value in dict(items):
            continue
        labels.add(label)
        items.append((member.value, label))
    if STOP not in dict(items):
        items.append((STOP, STOP_LABEL))
    return Menu(tuple(items))


@dataclass(frozen=True)
class Verdict:
    """A chooser's answer. `alongside` is the answer a shadowed chooser gave but was not acted on."""

    choice: str
    source: str
    confidence: float = 1.0
    probabilities: Mapping[str, float] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    alongside: Verdict | None = None
    blocked: str | None = None


class Decider:
    """Picks one value from a menu given redacted state."""

    def choose(self, state: Mapping[str, Any], menu: Menu, instructions: str) -> Verdict:
        raise NotImplementedError


@dataclass(frozen=True)
class Rule(Decider):
    """The rule-based code a site already has, wrapped: a pure function of the state and the menu's values."""

    fn: Callable[[Mapping[str, Any], Sequence[str]], str]

    def choose(self, state, menu, instructions):
        choice = self.fn(state, [value for value, _ in menu.items])
        if False:
            raise JevError("the rule chose a value that is not on the menu")
        return Verdict(choice, "rule")


@dataclass(frozen=True)
class Shadow(Decider):
    """Acts on the rule and records what Jev would have chosen; a Jev failure never reaches the caller."""

    rule: Decider
    jev: Decider

    def choose(self, state, menu, instructions):
        acted = self.rule.choose(state, menu, instructions)
        try:
            return replace(acted, alongside=self.jev.choose(state, menu, instructions))
        except JevError:
            return acted


@dataclass(frozen=True)
class Selective(Decider):
    """Lets only explicitly listed, sufficiently confident Jev choices pass; other Jev answers escalate."""

    rule: Decider
    jev: Decider
    choices: frozenset[str]

    def choose(self, state, menu, instructions):
        rule = self.rule.choose(state, menu, instructions)
        try:
            answer = self.jev.choose(state, menu, instructions)
        except JevError:
            return Verdict(STOP, "jev", confidence=0.0, alongside=rule, blocked="Jev request failed")
        if answer.choice not in self.choices:
            return replace(answer, alongside=rule, blocked="choice is outside this rollout's allow-list")
        return replace(answer, alongside=rule)


def agreement_allows(records: Sequence[Mapping[str, Any]], point: str, choices: frozenset[str], minimum: int = 20) -> bool:
    """Requires twenty eligible historical choices, over 95% rule agreement, and no wrong outcome."""
    eligible = [r for r in records if r.get("point") == point and r.get("jev") in choices and r.get("rule") is not None]
    if len(eligible) < minimum:
        return False
    return (sum(r["jev"] == r["rule"] for r in eligible) / len(eligible) > 0.95
            and not any(r.get("jev_outcome") == "wrong" for r in eligible))


Transport = Callable[[str, Mapping[str, str], bytes], bytes]


class Transient(Exception):
    """A transport failure worth retrying: HTTP 429 or 5xx."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes) -> bytes:
    """POST without following redirects, so the bearer token never goes to another host."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 or 500 <= e.code <= 599:
            raise Transient(str(e.code)) from None
        raise JevError(f"Jev answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError):
        raise JevError("Jev request failed in transit") from None


def read_key(path: str | None = None) -> str:
    """The API key as data from the file `NZELA_JEV_KEY_FILE` names."""
    path = path or os.environ.get("NZELA_JEV_KEY_FILE") or DEFAULT_KEY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except (OSError, UnicodeError):
        raise JevError("cannot read the Jev key file") from None
    if not key:
        raise JevError("the Jev key file is empty")
    return key


class TypeSafe(Decider):
    """Jev through the TypeSafe API. The key is read on first use and kept only in this object."""

    def __init__(self, transport: Transport = urllib_transport, key: Callable[[], str] = read_key,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._transport, self._key, self._sleep, self._clock = transport, key, sleep, clock
        self._token: str | None = None

    def choose(self, state, menu, instructions):
        if self._token is None:
            self._token = self._key()
        body = json.dumps({"model": "jev-latest", "state": state, "questions": {"next_action": {
            "type": "choice", "instructions": instructions, "criteria": menu.labels}}}, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"}
        started = self._clock()
        raw = self._post(headers, body)
        return validate(raw, menu, round((self._clock() - started) * 1000))

    def _post(self, headers: Mapping[str, str], body: bytes) -> bytes:
        for attempt in range(ATTEMPTS):
            try:
                return self._transport(ENDPOINT, headers, body)
            except Transient:
                if attempt == ATTEMPTS - 1:
                    break
                self._sleep(min(0.5 * 2 ** attempt, 2.0))
        raise JevError("Jev kept answering 429 or 5xx")


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError(f"Jev returned an invalid {what}")
    return float(value)


def validate(raw: bytes, menu: Menu, latency_ms: int) -> Verdict:
    """The response as a Verdict, or a JevError when anything in it is not on the menu or is not a probability."""
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise JevError("Jev returned invalid JSON") from None
    model = response.get("model") if isinstance(response, dict) else None
    if not isinstance(model, str) or not model.startswith(MODEL_PREFIX):
        raise JevError("Jev response named an unexpected model")
    answers = response.get("answers")
    answer = answers.get("next_action") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevError("Jev response is missing the choice")
    choice, probabilities = answer.get("choice"), answer.get("probabilities")
    if not isinstance(choice, str) or choice not in menu.labels:
        raise JevError("Jev chose an id that is not on the menu")
    if not isinstance(probabilities, dict) or set(probabilities) != set(menu.labels):
        raise JevError("Jev returned probabilities that do not match the menu")
    checked = {k: _number(v, "probability") for k, v in probabilities.items()}
    if not math.isclose(sum(checked.values()), 1.0, abs_tol=0.01):
        raise JevError("Jev probabilities do not sum to one")
    return Verdict(choice, "jev", _number(answer.get("confidence"), "confidence"), checked, latency_ms, model)


@dataclass(frozen=True)
class Decision:
    """What the caller does: execute `action` if there is one, otherwise raise `escalation` as needs-attention."""

    id: str
    action: enum.Enum | None
    escalation: str | None
    record: Mapping[str, Any]


class Log:
    """decisions.jsonl under the fleet home, one line per call."""

    def __init__(self, home: str = ""):
        self.path = os.path.join(home or config.HOME, "jev", "decisions.jsonl")

    def read(self) -> list[dict]:
        with self._locked(fcntl.LOCK_SH):
            return self._records()

    def _records(self) -> list[dict]:
        # A writer killed mid-line leaves a fragment; one torn record must not stop every later decision.
        records = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return [r for r in records if isinstance(r, dict)]

    @contextlib.contextmanager
    def _locked(self, mode: int) -> Iterator[None]:
        # set_outcomes rewrites the file; without the lock an append landing mid-rewrite is lost or torn.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, mode)
            yield

    def append(self, record: Mapping[str, Any]) -> None:
        with self._locked(fcntl.LOCK_EX), open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def set_outcome(self, decision_id: str, outcome: str, jev_outcome: str | None = None) -> bool:
        """Fills the outcomes of one decision; False when the id is not in the log."""
        return self.set_outcomes({decision_id: (outcome, jev_outcome)}) > 0

    def set_outcomes(self, outcomes: Mapping[str, tuple[str, str | None]]) -> int:
        """Fills `outcome` (ok, wrong or unknown: what happened to the choice that acted) and `jev_outcome` (the same
        question asked of Jev's own choice) in one rewrite. Returns how many decisions were found."""
        with self._locked(fcntl.LOCK_EX):
            records = self._records()
            hit = 0
            for r in records:
                if r.get("id") in outcomes:
                    r["outcome"], r["jev_outcome"] = outcomes[r["id"]]
                    hit += 1
            if hit:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
                os.replace(tmp, self.path)
            return hit


def _jev_side(verdict: Verdict) -> Verdict | None:
    return verdict if verdict.source == "jev" else verdict.alongside


def _escalation(point: Point, verdict: Verdict) -> str | None:
    """Why the chosen value must not be executed, for the verdict that would act."""
    if verdict.source != "jev":
        return None
    if verdict.blocked:
        return verdict.blocked
    if verdict.choice == STOP:
        return "Jev chose stop"
    if verdict.confidence < point.floor:
        return f"confidence {verdict.confidence:.2f} is below the floor {point.floor:.2f}"
    if verdict.choice in point.irreversible:
        return "an irreversible choice needs an approval"
    return None


def decide(point: Point, decider: Decider, state: Mapping[str, Any], options: Sequence[tuple[enum.Enum, str]],
           instructions: str, log: Log, now: datetime.datetime) -> Decision:
    """One call: redact the state, build the menu, ask, apply the floor, log. A Jev failure becomes an escalation, never a default action."""
    menu = build_menu(point, options)
    sent = redact.tree({k: v for k, v in state.items() if k in point.allowed_state})
    failure = None
    try:
        verdict = decider.choose(sent, menu, instructions)
    except JevError as e:
        verdict, failure = None, str(e)
    escalation = failure or _escalation(point, verdict)
    jev = _jev_side(verdict) if verdict else None
    decision_id = f"d-{now.strftime('%Y%m%d-%H%M%S')}-{len(log.read()) + 1:04d}"
    record = {
        "id": decision_id, "point": point.name, "mode": point.mode.value, "state": sent, "menu": menu.labels,
        "rule": verdict.choice if verdict and verdict.source == "rule" else verdict.alongside.choice if verdict and verdict.alongside and verdict.alongside.source == "rule" else None,
        "jev": jev.choice if jev else None, "confidence": jev.confidence if jev else None,
        "probabilities": dict(jev.probabilities) if jev else None, "latency_ms": jev.latency_ms if jev else None,
        "floor_miss": bool(jev and jev.confidence < point.floor), "escalation": escalation,
        "acted": verdict.source if verdict and not escalation else None, "outcome": None, "jev_outcome": None,
    }
    log.append(record)
    action = point.menu(verdict.choice) if verdict and not escalation else None
    return Decision(decision_id, action, escalation, record)


def replay(record: Mapping[str, Any], points: Mapping[str, tuple[Point, Rule]], live: Decider | None = None) -> dict:
    """Re-run a logged decision from its logged state against the current rule and, when `live` is given, Jev."""
    entry = points.get(record["point"])
    if entry is None:
        raise JevError(f"no decision point named {record['point']!r} is registered")
    _, rule = entry
    menu = Menu(tuple(record["menu"].items()))
    out = {"id": record["id"], "logged_rule": record["rule"], "logged_jev": record["jev"], "rule_now": rule.choose(record["state"], menu, "").choice}
    if live is not None:
        out["jev_now"] = live.choose(record["state"], menu, "Choose the next action.").choice
    return out


def summarise(records: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """Per point: calls, how often Jev matched the rule, its mean confidence, floor misses, stops, outcomes of the rule
    and of Jev's own choice, and every disagreement with what became of each side."""
    report: dict[str, dict] = {}
    for r in records:
        row = report.setdefault(r["point"], {"calls": 0, "compared": 0, "agreed": 0, "confidence": 0.0, "floor_misses": 0, "stops": 0,
                                             "outcomes": {}, "jev_outcomes": {}, "disagreements": []})
        row["calls"] += 1
        if r["rule"] is not None and r["jev"] is not None:
            row["compared"] += 1
            row["agreed"] += r["rule"] == r["jev"]
            row["confidence"] += r["confidence"]
            if r["rule"] != r["jev"]:
                row["disagreements"].append({k: r.get(k) for k in ("id", "rule", "jev", "confidence", "outcome", "jev_outcome")})
        row["floor_misses"] += bool(r["floor_miss"])
        row["stops"] += r["jev"] == STOP
        if r["outcome"] and r["acted"]:
            row["outcomes"].setdefault(r["acted"], {}).setdefault(r["outcome"], 0)
            row["outcomes"][r["acted"]][r["outcome"]] += 1
        if r.get("jev_outcome"):
            row["jev_outcomes"][r["jev_outcome"]] = row["jev_outcomes"].get(r["jev_outcome"], 0) + 1
    return report


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(counts.items())) or "none yet"


def format_report(report: Mapping[str, Mapping], disagreements: int = 10) -> str:
    """One paragraph per point, then up to `disagreements` of its disagreements, newest first."""
    lines = []
    for name, row in sorted(report.items()):
        compared = row["compared"]
        agreement = f"{row['agreed']}/{compared} ({100 * row['agreed'] // compared}%), mean confidence {row['confidence'] / compared:.2f}" if compared else "no comparison"
        rule = "; ".join(f"{who} {_counts(o)}" for who, o in sorted(row["outcomes"].items())) or "none yet"
        lines.append(f"{name}: {row['calls']} calls, agreement {agreement}, floor misses {row['floor_misses']}, stops {row['stops']}")
        lines.append(f"  outcomes of the acted choice: {rule}; of Jev's choice: {_counts(row['jev_outcomes'])}")
        for d in reversed(row["disagreements"][-disagreements:]):
            lines.append(f"  {d['id']}: rule {d['rule']} ({d['outcome'] or 'open'}), Jev {d['jev']} at {d['confidence']:.2f} ({d['jev_outcome'] or 'open'})")
    return "\n".join(lines) or "no decisions logged"


def _time_of(record: Mapping[str, Any]) -> str:
    return record["id"][2:17]


def within(records: Sequence[Mapping[str, Any]], since: str | None, until: str | None, point: str | None,
           now: datetime.datetime) -> list[Mapping[str, Any]]:
    """The records from `since` up to `until` (30m, 2h, 7d or a UTC time) and, when given, of one point. Raises ValueError on a time it cannot read."""
    lower = event_log.parse_since(since, now).strftime("%Y%m%d-%H%M%S") if since else ""
    upper = event_log.parse_since(until, now).strftime("%Y%m%d-%H%M%S") if until else "~"
    return [r for r in records if lower <= _time_of(r) <= upper and (point is None or r["point"] == point)]


def report_command(args, log: Log | None = None, now: datetime.datetime | None = None) -> int:
    """`nzfleet jev report [--since 7d] [--until 1h] [--point NAME] [--disagreements N]`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        records = within((log or Log()).read(), args.since, args.until, args.point, now)
    except ValueError as e:
        print(e)
        return 2
    print(format_report(summarise(records), args.disagreements))
    return 0


def replay_command(args, log: Log | None = None, points: Mapping[str, tuple[Point, Rule]] | None = None) -> int:
    """`nzfleet jev replay <id> [--live]`."""
    found = [r for r in (log or Log()).read() if r["id"] == args.id]
    if not found:
        print(f"no decision {args.id}")
        return 1
    try:
        print(json.dumps(replay(found[0], POINTS if points is None else points, TypeSafe() if args.live else None), indent=2, sort_keys=True))
    except JevError as e:
        print(e)
        return 1
    return 0


POINTS: dict[str, tuple[Point, Rule]] = {}
