"""The checks the changes since a base commit call for."""

from __future__ import annotations

import subprocess

from . import areas


def changed_paths(base: str) -> list | None:
    """The paths changed from `base` to HEAD, both sides of a rename, or None when git cannot say."""
    done = subprocess.run(["git", "diff", "--no-renames", "--name-only", f"{base}...HEAD"], capture_output=True, text=True)
    return done.stdout.splitlines() if done.returncode == 0 else None


def since(base: str, deployable: bool = False) -> tuple:
    """The areas for the changes since `base`; every area when the base is empty, unknown or has no readable diff.

    `deployable` is for main: the deploy job downloads this run's apk and the image it built, so those
    jobs run whether or not anything under them moved.
    """
    paths = changed_paths(base) if base else None
    if paths is None:
        return areas.ALL
    found, _ = areas.select(paths, "change")
    extra = ("android", "image", "deploy") if deployable else ()
    return tuple(dict.fromkeys((*found, *extra)))
