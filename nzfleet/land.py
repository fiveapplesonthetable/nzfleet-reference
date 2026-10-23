"""`nzfleet land <train>`: merge a verified train in order and check that main is the tree that was verified."""

from __future__ import annotations

import subprocess
import time

from . import mail, owner_tick, state, tick, train, world

ATTEMPTS = 12  # GitHub recomputes the next PR's mergeability for a few seconds after a merge
PAUSE = 5.0


def pause() -> None:
    time.sleep(PAUSE)


def _merge(n: int) -> None:
    for attempt in range(ATTEMPTS):
        try:
            return world.merge_pr(n)
        except world.Failed:
            if attempt == ATTEMPTS - 1:
                raise
            pause()


def _post(line: str) -> None:
    """Loud on purpose: a comment on the Merge queue issue, an email, and the daily digest."""
    found = world.issue(tick.QUEUE_TITLE)
    if found:
        world.comment(found["number"], line)
    mail.send("nzela: a train did not land as verified", line)
    owner_tick.news(line)


def _finish(train_id: str, status: str, why: str, merged: list) -> None:
    with state.change() as s:
        s["trains"] = train.finished(s["trains"], train_id, status, why)
        s["main_seen"] = world.main_sha()
        for n in merged:
            s["merged_at"].setdefault(str(n), time.time())


def land(train_id: str) -> int:
    t = state.read()["trains"].get(train_id)
    if t is None:
        print(f"no train {train_id}")
        return 1
    world.fetch([])
    heads = {p["number"]: p["headRefOid"] for p in world.open_prs()}
    reason = train.refusal(t, world.main_sha(), heads)
    if reason:
        print(f"refused: {reason}")
        return 1
    with state.change() as s:
        s["trains"][train_id]["status"] = train.LANDING
    merged, n = [], 0
    try:
        for n, _ in t["members"]:
            world.mark_ready(n)
            _merge(n)
            merged.append(n)
        world.fetch([])
        wrong = train.mismatch(t, world.main_tree())
    except (world.Failed, subprocess.TimeoutExpired) as e:
        why = f"stopped at #{n} after merging {' '.join(f'#{m}' for m in merged) or 'nothing'}: {e}"
        _finish(train_id, "stopped", why, merged)
        _post(f"Train {train_id} {why}")
        return 1
    if wrong:
        _finish(train_id, "mismatch", wrong, merged)
        _post(wrong)
        return 1
    _finish(train_id, "landed", "", merged)
    print(f"train {train_id} landed: {' '.join(f'#{n}' for n in merged)}; main's tree is the verified one")
    return 0
