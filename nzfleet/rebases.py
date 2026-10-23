"""Rebases that need hands: which one goes to an agent next, and what it is told."""

from __future__ import annotations

from . import landed, runs

TRAIN_BRANCH_PREFIX = "train-"


def in_open_train(key: str, members: set) -> bool:
    """Whether a hands task targets a PR already carried by an open train."""
    number = runs.base_key(key).removeprefix("hands-")
    return key.startswith("hands-") and number.isdigit() and int(number) in members


def targets_barred(key: str, barred: set) -> bool:
    """Whether a hands task, or a resumed attempt of one, is for a PR in `barred`."""
    number = runs.base_key(key).removeprefix("hands-")
    return key.startswith("hands-") and number.isdigit() and int(number) in barred


def hands_task(n: int, hands: dict) -> str:
    """The brief for the agent that rebases PR `n`, which no longer applies to its base."""
    parent = hands.get("parent")
    where = ", ".join(hands["files"]) or "the files it shares with the pull requests ahead of it in the queue"
    return (f"PR #{n} no longer applies to its base. Rebase it onto "
            f"{'PR #' + str(parent) + ' head' if parent else 'upstream/main'} as plain commits, "
            f"resolving conflicts in {where} by keeping both sides' intent, regenerate any "
            f"screenshots the change alters, run the checks for the areas it touches, and push with "
            f"--force-with-lease.")


def barred(open_prs: list, train_prs: dict, in_flight: set, held: dict, siblings: frozenset = frozenset()) -> set:
    """The open PRs whose rebase must not start or resume: drafts, held ones, ones that already merge, and anything a train owns.

    A PR in `siblings` merges into main alone but not after the PRs queued ahead of it, so GitHub's MERGEABLE does not bar it."""
    out = set(in_flight) | {int(n) for n in landed.landed_members(train_prs, set())} | {int(n) for n in train_prs}
    for key in held:
        base = runs.base_key(key)
        number = base.removeprefix("hands-")
        if base.startswith("hands-") and number.isdigit():
            out.add(int(number))
    for pr in open_prs:
        n = pr["number"]
        if pr["isDraft"] or (pr["mergeable"] == "MERGEABLE" and n not in siblings) or f"hands-{n}" in held or pr["headRefName"].startswith(TRAIN_BRANCH_PREFIX):
            out.add(n)
    return out


def next_hands(hands: dict, agents: dict, running: int, limit: int, busy: bool, barred: set | None = None):
    """The PR number and brief of the next rebase to hand to an agent, or None: lowest PR first, while there is room.

    A barred PR is skipped. A rebase already running, or already tried for this same brief, is not started again.
    """
    if busy or running >= limit:
        return None
    barred = barred or set()
    for n in sorted(hands, key=int):
        if int(n) in barred:
            continue
        task = hands_task(int(n), hands[n])
        known = agents.get(f"hands-{n}")
        if not known or (known["status"] != "running" and known.get("task") != task):
            return int(n), task
    return None
