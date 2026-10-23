"""What the agents cost, priced at list rates from transcript usage, as pure functions of message records."""

from __future__ import annotations

import collections

HOUR, DAY, WEEK = 3600, 24 * 3600, 7 * 24 * 3600

# USD per million tokens: input, output, cache read. A cache write is billed at twice the input rate.
RATES = {
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50), "claude-opus-4-8": (5, 25, 0.50),
    "claude-sonnet-5-5": (2, 10, 0.20), "claude-sonnet-5": (2, 10, 0.20),
    "claude-haiku-4-5-20251001": (1, 5, 0.10), "claude-haiku-5-5": (0.1, 0.5, 0.01),
    "claude-fable-5-1": (10, 50, 0.25),
}
PLACES = ("orchestrator", "workers", "subagents")


def price(model: str, usage: dict) -> float:
    """Dollars for one message's usage; a model without a listed rate costs nothing rather than a guess."""
    rate_in, rate_out, rate_read = RATES.get(model, (0, 0, 0))
    tokens = {k: usage.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    return (tokens["input_tokens"] * rate_in + tokens["output_tokens"] * rate_out + tokens["cache_read_input_tokens"] * rate_read
            + tokens["cache_creation_input_tokens"] * rate_in * 2) / 1e6


def once(records: list) -> list:
    """The records with each message id counted once; a resumed session writes the same message again."""
    seen, out = set(), []
    for r in records:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def _total(records: list) -> float:
    return round(sum(r["cost"] for r in records), 2)


def _by(records: list, field: str) -> dict:
    out: dict = collections.defaultdict(float)
    for r in records:
        out[r[field]] += r["cost"]
    return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def summary(records: list, now: float, day_start: float, pr_of_key: dict) -> dict:
    """Spend in the last hour, today and the last seven days; by model and place; per PR; and the five biggest keys today."""
    unique = once(records)
    week = [r for r in unique if now - r["at"] < WEEK]
    today = [r for r in week if r["at"] >= day_start]
    keyed = [r for r in today if r["key"]]
    per_pr: dict = collections.defaultdict(float)
    for r in week:
        if r["key"] in pr_of_key:
            per_pr[pr_of_key[r["key"]]] += r["cost"]
    return {"hour": _total([r for r in week if now - r["at"] < HOUR]), "today": _total(today), "week": _total(week),
            "by_model": _by(week, "model"), "by_place": _by(week, "place"),
            "top_today": [{"key": k, "usd": v} for k, v in list(_by(keyed, "key").items())[:5]],
            "per_pr": {str(n): round(v, 2) for n, v in sorted(per_pr.items())}}


def codex_runs(agents: dict, now: float, day_start: float) -> dict:
    """Codex runs started in the last hour and today. Their usage is not in Claude transcripts, so they are counted, not priced."""
    started = [a["started"] for a in agents.values() if a.get("provider") == "codex"]
    return {"codex_hour": sum(now - t < HOUR for t in started), "codex_today": sum(t >= day_start for t in started)}
