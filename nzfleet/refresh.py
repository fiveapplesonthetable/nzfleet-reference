"""Commits the screenshots a passing batch redrew on its merged tree, once, on top of the train."""

from __future__ import annotations

import os
import shutil

from . import pixels, shape, shots, world
from .config import UPSTREAM, WORK

# The area whose screenshots a source tree draws.
SOURCES = {"console": "frontend/admin/", "android": "frontend/android/"}
SHOTS = shots.REDRAWN


def redrawn(shots: str) -> dict:
    """The refreshed files a run left under `shots`, by area, as (state, path) pairs."""
    found = {}
    for area in SOURCES:
        try:
            lines = open(os.path.join(shots, area, "changed.txt")).read().splitlines()
        except OSError:
            continue
        found[area] = [tuple(line.split("\t")[:2]) for line in lines if line]
    return found


def report(shots: str) -> str:
    """The report pages a run wrote, one per area, for the batch summary."""
    return " ".join(os.path.join(shots, area, "index.html") for area in redrawn(shots))


def _bytes(shots_dir: str, area: str, side: str, path: str):
    try:
        with open(os.path.join(shots_dir, area, side, path), "rb") as f:
            return f.read()
    except OSError:
        return None


def _moved(shots_dir: str, area: str, state: str, path: str, golden: set) -> bool:
    if state != "changed":
        return True
    return pixels.moved(_bytes(shots_dir, area, "before", path), _bytes(shots_dir, area, "after", path), path in golden)


def moved(shots_dir: str, golden: set) -> list:
    """The (state, path) pairs of a run that moved by more than rendering noise. Golden shots are compared exactly.
    Every one belongs to the train: the merged tree drew them, so no member's diff has to account for a shot."""
    return [(state, path) for area, files in redrawn(shots_dir).items() for state, path in files
            if _moved(shots_dir, area, state, path, golden)]


def moved_summary(shots_dir: str, golden: set) -> str:
    """How many shots a run moved beyond rendering noise, and which screens they belong to."""
    return shots.summary([path for area, files in redrawn(shots_dir).items() for state, path in files
                          if _moved(shots_dir, area, state, path, golden)])


def moved_paths(shots_dir: str, golden: set) -> list:
    """The shots a run moved beyond rendering noise."""
    return [path for area, files in redrawn(shots_dir).items() for state, path in files
            if _moved(shots_dir, area, state, path, golden)]


def unowned(shots_dir: str, touched: list, golden: set) -> list:
    """The moved shots that no screen in `touched` draws."""
    drawn = [path for files in redrawn(shots_dir).values() for _, path in files]
    owned = set(drawn) - set(shots.stray(touched + drawn, set()))
    return [path for path in moved_paths(shots_dir, golden) if path not in owned]


def golden() -> set:
    """The golden-listed shot paths on main."""
    return shots.golden_names(lambda path: world.git("show", f"{world.MAIN}:{path}", check=False) or "")


def commit_train(sha: str, shots_dir: str, label: str, branch: str, golden: set) -> str:
    """Pushes one commit of the moved shots on top of `sha`, the last PR of the train, to `branch`. Returns the new
    head, or '' when nothing moved or the push failed."""
    mine = moved(shots_dir, golden)
    if not mine:
        return ""
    wt = os.path.join(WORK, f"nzf-shots-{label}")
    world.git("worktree", "remove", "--force", wt, check=False)
    world.git("worktree", "add", "-q", "--detach", wt, sha)
    try:
        for status, path in mine:
            target = os.path.join(wt, path)
            if status == "gone":
                if os.path.exists(target):
                    os.remove(target)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(shots_dir, shots.area_of(path), "after", path), target)
        shape._git(wt, "add", "-A", "--", *[p for _, p in mine])
        if shape._git(wt, "diff", "--cached", "--quiet").returncode == 0:
            return ""
        shape._git(wt, "commit", "-q", "-m", f"Screenshots for train {label}",
                   "-m", "Drawn on the tree the train merges to, so the goldens are the ones that land.")
        new = world.git("rev-parse", "HEAD", cwd=wt).strip()
        pushed = shape._git(wt, "push", "-q", "--no-verify", "origin", f"{new}:refs/heads/{branch}")
        return new if pushed.returncode == 0 else ""
    finally:
        world.git("worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)


def open_main_pr(shots_dir: str, main: str, day: str) -> int:
    """Opens a draft PR that refreshes the shots main itself draws differently. Returns its number."""
    branch = f"the-screenshots-match-main-{day}"
    wt = os.path.join(WORK, "nzf-shots-main")
    world.git("worktree", "remove", "--force", wt, check=False)
    world.git("worktree", "add", "-q", "--detach", wt, main)
    try:
        for area, files in redrawn(shots_dir).items():
            for status, path in files:
                target = os.path.join(wt, path)
                if status == "gone":
                    if os.path.exists(target):
                        os.remove(target)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(os.path.join(shots_dir, area, "after", path), target)
        shape._git(wt, "add", "-A", "--", *[p for files in SHOTS.values() for p in files])
        if shape._git(wt, "diff", "--cached", "--quiet").returncode == 0:
            return 0
        shape._git(wt, "commit", "-q", "-m", "The screenshots match what main draws",
                   "-m", "A batch drew main alone and its checked-in shots differed, so each later train would have reported "
                         "them as its own. This is the one refresh; the report is the list of what moved.")
        if shape._git(wt, "push", "-q", "--no-verify", "-f", "origin", f"HEAD:refs/heads/{branch}").returncode != 0:
            return 0
        url = world.gh("pr", "create", "-R", UPSTREAM, "--head", f"{world.origin_repository().split('/')[0]}:{branch}", "--base", "main",
                       "--draft", "--title", "The screenshots match what main draws",
                       "--body", "Main's checked-in shots differ from what a clean render of main draws. "
                                 "Opened by nzfleet, at most once a day.").strip().splitlines()[-1]
        return int(url.rsplit("/", 1)[1])
    finally:
        world.git("worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
