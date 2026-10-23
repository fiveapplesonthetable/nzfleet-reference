"""Reads agent transcripts for the message records `costs` prices, remembering how far into each file it has read."""

from __future__ import annotations

import datetime
import json
import os
import time

from . import costs

KEEP = costs.WEEK + costs.DAY
BUDGET = 20.0  # seconds one pass may read; the rest waits for the next tick


def project_slug(path: str) -> str:
    """The directory name Claude Code gives a working directory."""
    return path.replace("/", "-").replace(".", "-")


def classify(rel: str, repo_slug: str, worktrees: dict) -> tuple:
    """(place, agent key) for a transcript path relative to the projects directory; `worktrees` maps a slug to a key."""
    project = rel.split("/", 1)[0]
    if "/subagents/" in rel:
        return "subagents", worktrees.get(project, "")
    if project == repo_slug:
        return "orchestrator", ""
    return "workers", worktrees.get(project, "")


def _parse(line: str, place: str, key: str):
    try:
        d = json.loads(line)
        if d.get("type") != "assistant":
            return None
        m = d["message"]
        at = datetime.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        usage = m.get("usage") or {}
        context = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return {"id": m["id"], "at": at, "model": m.get("model", ""), "cost": costs.price(m.get("model", ""), usage),
                "place": place, "key": key, "ctx": context}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _read_new(path: str, entry: dict, place: str, key: str) -> dict:
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        data = f.read()
    end = data.rfind(b"\n") + 1
    found = [r for r in (_parse(line, place, key) for line in data[:end].decode(errors="replace").splitlines()) if r]
    return {**entry, "offset": entry["offset"] + end, "records": entry["records"] + found}


def scan(projects: str, repo_slug: str, worktrees: dict, cache: dict, now: float | None = None) -> dict:
    """The cache with every recent transcript read up to its end, or as far as the time budget allowed."""
    now = now or time.time()
    started, out = time.monotonic(), {}
    for root, _, names in os.walk(projects):
        for name in names:
            path = os.path.join(root, name)
            if not name.endswith(".jsonl"):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if now - stat.st_mtime > KEEP:
                continue
            entry = cache.get(path, {"offset": 0, "records": []})
            if stat.st_size < entry["offset"]:
                entry = {"offset": 0, "records": []}
            if stat.st_size > entry["offset"] and time.monotonic() - started < BUDGET:
                place, key = classify(os.path.relpath(path, projects), repo_slug, worktrees)
                try:
                    entry = _read_new(path, entry, place, key)
                except OSError:
                    pass
            out[path] = entry
    return out


def records(cache: dict) -> list:
    return [r for entry in cache.values() for r in entry["records"]]
