"""Pull requests a merge-check left out: how the verdict names them and which of them need a rebase."""

from __future__ import annotations

CONFLICT = "CONFLICT"


def reason(skip: dict) -> str:
    """One phrase for why a PR was left out, naming the conflicted files when there were any."""
    if skip.get("why") == CONFLICT:
        files = ", ".join(skip.get("files", []))
        return f"CONFLICT in {files}" if files else CONFLICT
    return skip.get("why") or "not merged, reason unknown"


def describe(skipped: list, skips: dict) -> str:
    """`skipped #1174 (CONFLICT in a.py); #9 (its head moved ...)`, or nothing when none were skipped."""
    if not skipped:
        return ""
    return "skipped " + "; ".join(f"#{n} ({reason(skips.get(str(n), {}))})" for n in skipped)


def outcome(outcome: str, skipped: list) -> str:
    """A green run that left a member out is `partial`: its passed members are ready, the rest is not."""
    return "partial" if outcome == "green" and skipped else outcome


def rebases(skipped: list, skips: dict, heads: dict, hands: dict) -> dict:
    """Hands records for the conflicted PRs that have none at their current head."""
    queued = {}
    for n in skipped:
        skip = skips.get(str(n), {})
        if skip.get("why") != CONFLICT or hands.get(str(n), {}).get("sha") == heads[n]:
            continue
        queued[str(n)] = {"sha": heads[n], "files": skip.get("files") or ["the files it conflicts in"], "parent": None}
    return queued
