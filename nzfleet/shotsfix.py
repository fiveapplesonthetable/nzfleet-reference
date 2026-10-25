"""Why a golden screenshot moved, and what to do about it: settle it, or say what broke."""

from __future__ import annotations

import enum
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from . import pixels, refresh, shape, shots, world
from .config import WORK

GOLDEN_MOVED = "a golden screenshot moved"
# What draws the shots rather than what they show: build files, the renderer's config, the screenshot tests, the theme.
SETUP = re.compile(r"(^|/)(build\.gradle(\.kts)?|settings\.gradle(\.kts)?|libs\.versions\.toml|gradle\.properties"
                   r"|vite\.config\.\w+|playwright\.config\.\w+|screenshots\.mjs|shot-settle\.mjs|shot-report\.sh"
                   r"|Dockerfile)$|/screenshot/|/theme/|/design/|^\.github/workflows/")
# More than this share of the catalogue moving at once is the canvas, not the screens.
MOST = 0.5


class Kind(enum.Enum):
    CANVAS = "canvas"
    INTENDED = "intended"
    DRIFT = "drift"


@dataclass(frozen=True)
class Golden:
    """A golden shot that moved: its path and the picture sizes either side (None when the file is gone)."""
    path: str
    before: Optional[tuple]
    after: Optional[tuple]
    share: Optional[float]


@dataclass(frozen=True)
class Verdict:
    path: str
    kind: Kind
    line: str


def _size(size) -> str:
    return "none" if size is None else f"{size[0]}x{size[1]}"


def _setup_note(touched: list) -> str:
    found = [f for f in touched if SETUP.search(f)]
    if not found:
        return "nothing in the diff touches build or test setup, so look at the runner image and the toolchain"
    return "the diff touches build or test setup: " + ", ".join(found[:5])


def canvas_line(moved: list, touched: list) -> str:
    """Names the canvas break: what every shot is now, what it was, and what in the diff could have done it."""
    now = sorted({_size(g.after) for g in moved})
    was = sorted({_size(g.before) for g in moved})
    if len(now) == 1 and len(was) == 1 and now != was:
        what = f"every shot is {now[0]}, expected {was[0]}"
    else:
        what = f"shots are {', '.join(now)}, expected {', '.join(was)}" if now != was else \
            f"{len(moved)} golden shots moved at once at {now[0]}"
    return f"rendering setup broke, not a screen: {what}; {_setup_note(touched)}"


def _canvas_changed(moved: list, moved_total: int, catalogue: int) -> bool:
    resized = any(g.before != g.after for g in moved)
    return resized or (catalogue > 0 and moved_total > MOST * catalogue)


def classify(moved: list, moved_total: int, catalogue: int, touched: list) -> list:
    """One verdict per moved golden of one area. `moved_total` counts every shot that moved there and `catalogue`
    every shot it draws; `touched` is the ref's diff."""
    if not moved:
        return []
    if _canvas_changed(moved, moved_total, catalogue):
        line = canvas_line(moved, touched)
        return [Verdict(g.path, Kind.CANVAS, line) for g in moved]
    out = []
    for g in moved:
        if g.after is not None and not shots.stray(touched + [g.path], set()):
            out.append(Verdict(g.path, Kind.INTENDED, "the diff reaches this screen; redrawn golden committed"))
        elif g.after is None:
            out.append(Verdict(g.path, Kind.DRIFT, "the golden is no longer drawn and nothing in the diff removes it"))
        else:
            delta = "" if g.share is None else f", {g.share:.3%} of its channels differ"
            out.append(Verdict(g.path, Kind.DRIFT, f"nothing in the diff reaches this screen{delta}; not committed"))
    return out


def verdict(results: list) -> tuple:
    """(final line, exit status): non-zero when a golden moved for a reason a commit cannot settle."""
    if not results:
        return "no golden screenshot moved", 0
    kinds = {r.kind for r in results}
    if Kind.CANVAS in kinds:
        return "the canvas changed: fix the rendering setup, commit no screenshots", 1
    if Kind.DRIFT in kinds:
        return "a golden moved without a change to its screen: find what else changed the render, commit no screenshots", 1
    return f"{len(results)} golden screenshot(s) follow their screens' intended changes", 0


def lines(results: list) -> list:
    return [f"{r.kind.value:8} {r.path}: {r.line}" for r in results]


def _bytes(shots_dir: str, area: str, side: str, path: str) -> Optional[bytes]:
    try:
        with open(os.path.join(shots_dir, area, side, path), "rb") as f:
            return f.read()
    except OSError:
        return None


def read_goldens(shots_dir: str, golden: set) -> dict:
    """Per area, the goldens a report shows moved, and how many shots of that area moved in all."""
    moved = {(shots.area_of(p), p) for _, p in refresh.moved(shots_dir, golden)}
    found = {}
    for area, files in refresh.redrawn(shots_dir).items():
        goldens = []
        for state, path in files:
            if path not in golden or (area, path) not in moved:
                continue
            before, after = (None if state == "new" else _bytes(shots_dir, area, "before", path),
                             None if state == "gone" else _bytes(shots_dir, area, "after", path))
            goldens.append(Golden(path, pixels.size_of(before), pixels.size_of(after), pixels.share_changed(before, after)))
        found[area] = (goldens, sum(1 for a, _ in moved if a == area))
    return found


def _draw(wt: str, out: str, areas: list) -> None:
    for area in areas:
        subprocess.run(["scripts/checks.sh", area], cwd=wt, env=dict(os.environ, SHOT_REPORT=out), check=False)


def _resolve(ref: str) -> tuple:
    """(sha, branch to push, base to diff against). Main has no branch to push to."""
    if ref.isdigit():
        world.fetch([int(ref)])
        return world.head_of(int(ref)), world.branch_of(int(ref)), world.MAIN
    sha = world.git("rev-parse", ref).strip()
    if sha == world.main_sha():
        return sha, "", f"{sha}~1"
    return sha, ref.removeprefix("origin/"), world.MAIN


def _commit(wt: str, shots_dir: str, intended: list, branch: str, link: str) -> bool:
    for v in intended:
        target = os.path.join(wt, v.path)
        shutil.copyfile(os.path.join(shots_dir, shots.area_of(v.path), "after", v.path), target)
    shape._git(wt, "add", "-A", "--", *[v.path for v in intended])
    if shape._git(wt, "diff", "--cached", "--quiet").returncode == 0:
        return False
    shape._git(wt, "commit", "-q", "-m", "The golden screenshots follow the screens this change redraws",
               "-m", f"Each moved golden is one whose screen the diff touches, so the new picture is the intended one. Report: {link}")
    return shape._git(wt, "push", "-q", "--no-verify", "origin", f"HEAD:refs/heads/{branch}").returncode == 0


def fix(ref: str, report: str = "", report_url: str = "", areas: Optional[list] = None) -> int:
    """Classifies the golden shots that moved on `ref`, commits the intended ones to its branch, prints a line each."""
    sha, branch, base = _resolve(ref)
    wt = os.path.join(WORK, f"nzf-shotsfix-{sha[:8]}")
    world.git("worktree", "remove", "--force", wt, check=False)
    world.git("worktree", "add", "-q", "--detach", wt, sha)
    try:
        shots_dir = report or os.path.join(WORK, f"nzf-shotsfix-{sha[:8]}-report")
        if not report:
            _draw(wt, shots_dir, areas or list(refresh.SOURCES))
        touched = world.git("diff", "--no-renames", "--name-only", f"{base}...{sha}").split()
        golden = shots.golden_names(lambda path: world.git("show", f"{sha}:{path}", check=False) or "")
        results = []
        for area, (goldens, moved_total) in read_goldens(shots_dir, golden).items():
            catalogue = len([p for p in world.git("ls-tree", "-r", "--name-only", sha, "--", shots.SHOT_DIRS[area]).split()
                             if p.endswith(".png")])
            results += classify(goldens, moved_total, catalogue, touched)
        for line in lines(results):
            print(line)
        final, status = verdict(results)
        intended = [r for r in results if r.kind is Kind.INTENDED]
        if intended and not branch:
            print("main takes a pull request, not a commit: open one with these goldens")
            status = 1
        elif intended:
            link = report_url or os.path.join(shots_dir, "index.html")
            pushed = _commit(wt, shots_dir, intended, branch, link)
            final += "; committed" if pushed else "; nothing to commit or the push failed"
            status = status or (0 if pushed else 1)
        print(final)
        return status
    finally:
        world.git("worktree", "remove", "--force", wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
