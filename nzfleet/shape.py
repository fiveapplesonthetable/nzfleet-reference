"""Replays a PR branch as its own commits on its base, so GitHub can merge what the batch checked."""

from __future__ import annotations

import os
import subprocess

from . import plan, world
from .config import GIT_EMAIL, GIT_NAME, WORK

AUTHOR = ["-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}"] if GIT_NAME else []

def own_commits(head: str, base: str) -> list:
    """The PR's non-merge commits that its base does not already have, even under another hash, oldest first."""
    out = world.git("rev-list", "--reverse", "--no-merges", "--cherry-pick", "--right-only", f"{base}...{head}")
    return out.split()


def _git(wt: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *AUTHOR, *args], cwd=wt, capture_output=True, text=True,
                          env=dict(os.environ, GIT_EDITOR="true"))


def _apply(wt: str, commit: str):
    """Cherry-picks one commit. Returns the files it conflicted on when any is written by hand; a conflict
    in generated files alone takes the base's copy, because the batch draws them again."""
    if _git(wt, "cherry-pick", "--allow-empty", commit).returncode == 0:
        return []
    files = world.git("diff", "--name-only", "--diff-filter=U", cwd=wt).split()
    if plan.generated_areas(files) is None:
        _git(wt, "cherry-pick", "--abort")
        return files
    for f in files:
        if _git(wt, "cat-file", "-e", f"HEAD:{f}").returncode == 0:
            _git(wt, "checkout", "--ours", "--", f)
            _git(wt, "add", "--", f)
        else:
            _git(wt, "rm", "-q", "--", f)
    if _git(wt, "cherry-pick", "--continue").returncode != 0:
        _git(wt, "cherry-pick", "--skip")
    return []


def replay(n: int, base: str, dry_run: bool = False):
    """Returns ('replayed', new_sha), ('clean', None) when nothing needed doing, or ('hands', files)."""
    head = world.head_of(n)
    commits = own_commits(head, base)
    wt = os.path.join(WORK, f"nzf-replay-{n}")
    world.git("worktree", "remove", "--force", wt, check=False)
    world.git("worktree", "add", "-q", "--detach", wt, base)
    try:
        for c in commits:
            conflicts = _apply(wt, c)
            if conflicts:
                return "hands", conflicts
        new = world.git("rev-parse", "HEAD", cwd=wt).strip()
        if world.git("rev-parse", f"{new}^{{tree}}").strip() == world.git("rev-parse", f"{head}^{{tree}}").strip() \
                and not world.has_merge_commits(head, base):
            return "clean", None
        if not dry_run:
            branch = world.branch_of(n)
            world.git("push", "-q", "--no-verify", f"--force-with-lease={branch}:{head}", "origin", f"{new}:refs/heads/{branch}", cwd=wt)
        return "replayed", new
    finally:
        world.git("worktree", "remove", "--force", wt, check=False)
        if os.path.isdir(wt):
            subprocess.run(["rm", "-rf", wt])
