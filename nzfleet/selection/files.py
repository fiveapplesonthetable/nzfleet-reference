"""Tracked files for the checks that scan them: an empty match or a git error is a failure, never an empty inventory."""

from __future__ import annotations

import subprocess


def tracked(label: str, specs: list) -> list:
    """The tracked files matching `specs`. Raises ValueError, naming `label`, when git fails or nothing matches."""
    done = subprocess.run(["git", "ls-files", "--", *specs], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError(f"{label}: git could not list tracked files")
    files = done.stdout.splitlines()
    if not files:
        raise ValueError(f"{label}: no tracked files match: {' '.join(specs)}")
    return files


def require(label: str, path: str) -> None:
    """Raises ValueError, naming `label`, unless `path` is tracked."""
    done = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError(f"{label}: expected tracked file is missing: {path}")
