"""A Jev choice over the cache directories janitor already knows how to remove."""

from __future__ import annotations

import datetime
import enum
import os
import shutil
from typing import Sequence

from . import jev, tidy


def clear_choice(caches: Sequence[tidy.Sized], chooser: jev.Decider, log: jev.Log,
                 now: datetime.datetime, running_batches: Sequence[str] = (), agents: int = 0) -> tuple[str | None, str | None]:
    """Returns an unlocked cache path only when Jev selects it at the 0.8 floor."""
    available = [(f"cache_{index}", cache) for index, cache in enumerate(caches) if not cache.held]
    names = {f"CLEAR_{name.upper()}": f"clear:{name}" for name, _cache in available}
    names.update(DEFER="defer_next_batch", NOTHING="nothing", STOP="stop")
    menu_type = enum.Enum("CacheChoice", names)
    point = jev.Point("disk.load", menu_type, jev.Consequence.STOPS_WORK,
                      frozenset({"free_root_gb", "free_agent_gb", "load", "caches", "running_batches", "agents"}))
    options = [(menu_type[f"CLEAR_{name.upper()}"], f"Clear unlocked cache {name} ({cache.gb} GB)")
               for name, cache in available]
    options.extend(((menu_type.DEFER, "Defer the next batch"), (menu_type.NOTHING, "Do nothing")))
    state = {"free_root_gb": _free_gb("/"),
             "free_agent_gb": _free_gb("/srv/fleet"),
             "load": round(os.getloadavg()[0], 1),
             "caches": [{"name": name, "gb": cache.gb, "locked": cache.held} for name, cache in available],
             "running_batches": list(running_batches), "agents": agents}
    decision = jev.decide(point, chooser, state, options,
                          "Choose a cache to clear only if free disk or load makes it useful. A cleared cache causes a cold build.",
                          log, now)
    if decision.action is None or not decision.action.value.startswith("clear:"):
        return None, decision.id
    selected = decision.action.value.removeprefix("clear:")
    return next((cache.path for name, cache in available if name == selected), None), decision.id


def _free_gb(path: str) -> int | None:
    """Free space under path, or None on a machine without that disk, such as a ci container."""
    try:
        return shutil.disk_usage(path).free // 2**30
    except FileNotFoundError:
        return None
