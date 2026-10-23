"""What a landed train means for the pull requests inside it, as pure functions of plain data."""

from __future__ import annotations

import re

_LANDED = re.compile(r"Landed in #(\d+)")


def comment(train_pr: int) -> str:
    return f"Landed in #{train_pr}"


def landed_in(comments: list):
    """The train a 'Landed in #N' comment names, or None."""
    return next((int(m.group(1)) for body in comments if (m := _LANDED.search(body))), None)


def newly_landed(train_prs: dict, merged: set) -> list:
    """Train PR numbers main has merged whose members have not been settled yet, lowest first."""
    return sorted(int(pr) for pr, record in train_prs.items() if int(pr) in merged and not record.get("landed"))


def abandoned(train_prs: dict, open_numbers: set, merged: set) -> list:
    """Train PR numbers closed without merging that still hold their members, lowest first."""
    return sorted(int(pr) for pr, record in train_prs.items()
                  if int(pr) not in open_numbers and int(pr) not in merged and not record.get("landed") and not record.get("abandoned"))


def landed_members(train_prs: dict, merged: set) -> set:
    """Every member of a train PR that main has merged."""
    return {n for pr, record in train_prs.items() if record.get("landed") or int(pr) in merged for n in record["members"]}


def retire_rebases(todo: list, hands: dict, members: list) -> tuple:
    """(todo, hands) without the queued rebase of each member: its change is on main, there is nothing to rebase."""
    keys = {f"rebase-{n}" for n in members}
    return [t for t in todo if t["key"] not in keys], {n: h for n, h in hands.items() if int(n) not in members}
