"""The checks the changes since a base commit call for."""

from __future__ import annotations

import subprocess

from . import areas

# Parts of an area that cost minutes and that a pull request runs only when its changes can break them.
# They are not areas: no check script selects on them, only the jobs they gate.
COSTLY = ("android-release", "console-browser")


def changed_paths(base: str) -> list | None:
    """The paths changed from `base` to HEAD, both sides of a rename, or None when git cannot say."""
    done = subprocess.run(["git", "diff", "--no-renames", "--name-only", f"{base}...HEAD"], capture_output=True, text=True)
    return done.stdout.splitlines() if done.returncode == 0 else None


def costly(found: tuple, paths: list, pull_request: bool, train: bool) -> tuple:
    """`found` with the release image and the costly parts set for this run.

    Main runs all of them. A train runs the image and the browser suite, and leaves the release APK to main, where
    R8 is off the path to a merge. Any other pull request runs each only when a path it touched is something it is
    built from.
    """
    full = train or not pull_request
    wanted_image = full or any(areas.IMAGE_INPUTS.search(p) for p in paths)
    out = [a for a in found if a != "image" or wanted_image]
    if pull_request and wanted_image and "image" not in out:
        out.append("image")
    if "android" in found and (not pull_request or (not train and any(areas.ANDROID_RELEASE_INPUTS.search(p) for p in paths))):
        out.append("android-release")
    console = [p for p in paths if p.startswith("frontend/admin/")]
    if "console" in found and (full or any(not areas.CONSOLE_BROWSER_IDLE.search(p) for p in console)):
        out.append("console-browser")
    return tuple(dict.fromkeys(out))


def since(base: str, deployable: bool = False, pull_request: bool = False, train: bool = False) -> tuple:
    """The areas for the changes since `base`; every area when the base is empty, unknown or has no readable diff.

    `deployable` is for main: the deploy job downloads this run's apk and the image it built, so those
    jobs run whether or not anything under them moved.
    """
    paths = changed_paths(base) if base else None
    if paths is None:
        return (*areas.ALL, *COSTLY)
    found, _ = areas.select(paths, "change")
    extra = ("android", "image", "deploy") if deployable else ()
    return costly(tuple(dict.fromkeys((*found, *extra))), paths, pull_request, train)
