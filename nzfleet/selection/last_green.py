"""The newest commit ci has passed on main that a commit descends from."""

from __future__ import annotations

import json
import subprocess


def newest(runs_json: str, head: str) -> str:
    """The first successful run's commit that is an ancestor of `head`, skipping `head` itself; "" when none.

    Skipping `head` lets a rerun of a green commit still compare against something. "" means every check.
    """
    try:
        runs = json.loads(runs_json).get("workflow_runs", [])
    except (ValueError, AttributeError):
        return ""
    for run in runs:
        sha = run.get("head_sha")
        if run.get("conclusion") != "success" or not sha or sha == head:
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", sha, head], capture_output=True).returncode == 0:
            return sha
    return ""


def resolve(rev: str) -> str:
    """The full commit `rev` names, or "" when git cannot say."""
    done = subprocess.run(["git", "rev-parse", rev], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""
