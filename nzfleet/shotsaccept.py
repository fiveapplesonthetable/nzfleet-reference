"""Copies the screenshots a batch drew into a branch, so an intended change to a golden lands without a redraw."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from typing import Callable

from . import refresh, shots, world
from .config import HOME


class Refused(Exception):
    pass


def run_dir(run: str) -> str:
    """A run's directory from its path, its exact name, or a batch id (the highest round of it)."""
    runs = os.path.join(HOME, "runs")
    if os.path.isdir(run):
        return run
    if os.path.isdir(os.path.join(runs, run)):
        return os.path.join(runs, run)
    rounds = glob.glob(os.path.join(glob.escape(runs), glob.escape(run) + "-r*"))
    rounds = [r for r in rounds if re.search(r"-r\d+$", r)]
    return max(rounds, key=lambda r: int(r.rsplit("-r", 1)[1]), default=os.path.join(runs, run))


def blobs(listing: str) -> dict:
    """The blob id of each path in `git ls-tree` output."""
    out = {}
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        out[path] = meta.split()[2]
    return out


def foreign(at_head: dict, at_run: dict) -> list:
    """The paths whose content in the run's tree is not what this checkout has."""
    return sorted(path for path, blob in at_head.items() if at_run.get(path) != blob)


def _check_drawn_from_here(run: str, base: str, git: Callable) -> None:
    try:
        with open(os.path.join(run, "result.json")) as f:
            tree = json.load(f).get("tree")
    except (OSError, ValueError):
        tree = None
    if not tree:
        raise Refused(f"{run} records no tree, so it cannot be shown to have drawn this checkout; pass --force to copy anyway")
    changed = git("diff", "--name-only", "--no-renames", git("merge-base", "HEAD", base).strip(), "HEAD").splitlines()
    files = [f for f in changed if f and not f.startswith(shots.GENERATED)]
    if not files:
        return
    other = foreign(blobs(git("ls-tree", "HEAD", "--", *files)), blobs(git("ls-tree", tree, "--", *files)))
    if other:
        raise Refused(f"the images in {run} were drawn from a tree that lacks this checkout's changes to "
                      f"{', '.join(other[:5])}, so they belong to another change. Pass --force to copy them anyway")


def _read(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def accept(run: str, checkout: str, areas: list, everything: bool, force: bool, git: Callable, base: str = world.MAIN) -> list:
    """Writes the run's after images into `checkout` and returns the paths written. Goldens only, unless `everything`."""
    golden = shots.golden_names(lambda path: (_read(os.path.join(checkout, path)) or b"").decode())
    if not force:
        _check_drawn_from_here(run, base, git)
    drawn = os.path.join(run, "shots")
    written = []
    for area, files in sorted(refresh.redrawn(drawn).items()):
        if area not in areas:
            continue
        for state, path in files:
            source, target = os.path.join(drawn, area, "after", path), os.path.join(checkout, path)
            if state == "gone" or (not everything and path not in golden):
                continue
            if _read(source) is None or _read(source) == _read(target) or not refresh.has_moved(drawn, area, state, path, golden):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            written.append(path)
    return written
