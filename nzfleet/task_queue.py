"""Keeps the dispatch queue small while retaining the last completed task in each area."""

from __future__ import annotations

import re

_OLD_KEYS = (("train-fix", re.compile(r"train-fix-(\d+)-.+")), ("rebase", re.compile(r"rebase-(\d+)")),
             ("fix-compile", re.compile(r"fix-compile-(\d+)")))


def typed(item: dict) -> dict:
    """The item with its `kind` and the `pr` it works on; an item queued before they were stored is read from its key."""
    if "kind" in item:
        return item
    for kind, pattern in _OLD_KEYS:
        found = pattern.fullmatch(item["key"])
        if found:
            return {**item, "kind": kind, "pr": int(found.group(1))}
    return {**item, "kind": "task", "pr": None}


def in_flight_members(s: dict) -> set:
    """The PR numbers carried by an open train or by a train pull request that is still open: one that landed or was closed has released them."""
    members = {number for record in s["trains"].values() if record["status"] == "open" for number, _sha in record["members"]}
    return members.union(*(record["members"] for record in s["train_prs"].values() if not record.get("landed") and not record.get("abandoned")))


def withdraw(todo: list, gone: dict) -> tuple:
    """Splits the queue into the items to keep and (item, why) for each whose PR is in `gone`, a map of PR number to why it is gone."""
    kept, withdrawn = [], []
    for item in todo:
        if item.get("pr") in gone:
            withdrawn.append((item, f"{item['kind']} for #{item['pr']}: {gone[item['pr']]}"))
        else:
            kept.append(item)
    return kept, withdrawn


def compact(todo: list, agents: dict, history: dict, area_heads: dict) -> tuple:
    """Moves finished task rows into history and returns the actionable queue and area heads."""
    waiting, archived, heads = [], dict(history), dict(area_heads)
    for item in todo:
        run = agents.get(item["key"], {})
        status = run.get("status")
        if status not in ("done", "retired"):
            waiting.append(item)
            continue
        archived[item["key"]] = {**item, "status": status, "finished": run.get("finished")}
        if item.get("area") and not item.get("now"):
            heads[item["area"]] = item["key"]
    return waiting, archived, heads


def decision_tasks(todo: list, history: dict, area_heads: dict) -> list:
    """The active queue plus each area's latest completed entry for owner decisions."""
    active = {item["key"] for item in todo}
    completed = [history[key] for key in dict.fromkeys(area_heads.values())
                 if key not in active and key in history]
    return [*completed, *todo]
