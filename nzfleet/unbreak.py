"""When a main-broken batch is finished with: its PRs are gone, or main has gone green since."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cleared:
    drop: tuple  # batch ids to forget
    release: frozenset  # open PR numbers to check again on the new main


def main_recovered(batch: dict, main_ci: dict, is_ancestor) -> bool:
    """True when main's last completed run is green and on a commit after the one the batch was checked against."""
    sha = main_ci.get("sha")
    return bool(main_ci.get("ok")) and bool(sha) and sha != batch["main"] and is_ancestor(batch["main"], sha)


def clear(batches: dict, open_numbers: set, main_ci: dict, is_ancestor) -> Cleared:
    drop, release = [], set()
    for bid, batch in batches.items():
        if batch["status"] != "main-broken":
            continue
        open_members = {int(n) for n, _ in batch["members"]} & open_numbers
        if not open_members:
            drop.append(bid)
        elif main_recovered(batch, main_ci, is_ancestor):
            drop.append(bid)
            release |= open_members
    return Cleared(tuple(drop), frozenset(release))
