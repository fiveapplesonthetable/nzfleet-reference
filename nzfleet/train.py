"""A train is the set of PRs a batch verified, in merge order, with the main it was checked on and the tree
that merging them must produce. Landing is allowed only while all of that still holds. Plain data in, plain data out."""

from __future__ import annotations

import hashlib

OPEN = "open"
LANDING = "landing"


def make(base: str, members: list, tree: str) -> dict:
    digest = hashlib.sha1(" ".join([base, tree, *(f"{n}@{sha}" for n, sha in members)]).encode()).hexdigest()[:8]
    return {"id": digest, "base": base, "members": [[n, sha] for n, sha in members], "tree": tree, "status": OPEN, "why": ""}


def open_for(trains: dict, numbers: list) -> str:
    """The id of the open train that merges exactly these PRs, or ''."""
    return next((t["id"] for t in trains.values() if t["status"] == OPEN and [n for n, _ in t["members"]] == numbers), "")


def with_train(trains: dict, train: dict) -> dict:
    """Adds the train; any other open one is replaced by it, so only one command is ever current."""
    kept = {k: ({**t, "status": "invalidated", "why": f"superseded by {train['id']}"} if t["status"] == OPEN else t)
            for k, t in trains.items() if k != train["id"]}
    return {**kept, train["id"]: train}


def refusal(train: dict, main: str, heads: dict) -> str:
    """Why the train may not land now, or '' when it may."""
    if train["status"] != OPEN:
        return f"train {train['id']} is {train['status']}" + (f": {train['why']}" if train["why"] else "")
    if train["base"] != main:
        return f"main is at {main[:8]}, not at {train['base'][:8]} where the train was verified"
    moved = [n for n, sha in train["members"] if heads.get(n) != sha]
    if moved:
        return f"{', '.join(f'#{n}' for n in moved)} changed since the train was verified"
    return ""


def after_outside_merge(trains: dict) -> dict:
    """Main moved without a train landing: no open train is verified against this main any more."""
    if any(t["status"] == LANDING for t in trains.values()):
        return trains
    return {k: ({**t, "status": "invalidated", "why": "main moved outside a train"} if t["status"] == OPEN else t)
            for k, t in trains.items()}


def finished(trains: dict, train_id: str, status: str, why: str = "") -> dict:
    """Records how a landing ended; every other open train was verified against a main that has now moved."""
    return {k: ({**t, "status": status, "why": why} if k == train_id else
                {**t, "status": "invalidated", "why": f"train {train_id} landed first"} if t["status"] == OPEN else t)
            for k, t in trains.items()}


def mismatch(train: dict, main_tree: str) -> str:
    """The loud sentence for a main whose tree is not the verified one, or ''."""
    if main_tree == train["tree"]:
        return ""
    return (f"main's tree after landing train {train['id']} is {main_tree[:12]}, "
            f"not the verified {train['tree'][:12]}: what landed is not what was checked")
