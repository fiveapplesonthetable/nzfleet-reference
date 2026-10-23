"""`nzfleet pr`: pushes the branch and opens or updates its draft PR, once the proof covers exactly what is committed."""

from __future__ import annotations

import json
import os
import re
import tempfile

from . import config, plan, procs, shots, world
from .config import UPSTREAM


def _fork_owner(wt: str) -> str:
    url = world.git("remote", "get-url", "origin", cwd=wt).strip()
    m = re.search(r"github\.com[:/]([^/]+)/", url)
    return m.group(1) if m else ""


def pr(title: str, description: str, dry_run: bool, shots_anyway: bool = False, say=print) -> int:
    wt = procs.root()
    if not procs.load_env(wt) or "NZ_BASE_SHA" not in os.environ:
        say("this is not a worktree made by `nzfleet start`: there is no .nzenv here")
        return 1
    base = os.environ["NZ_BASE_SHA"]
    branch = world.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).strip()
    if branch == "HEAD":
        say("HEAD is detached. Run: git switch " + os.environ.get("NZ_SLUG", "<your branch>"))
        return 1
    files = world.git("diff", "--no-renames", "--name-only", f"{base}..HEAD", cwd=wt).split()
    log = world.git("log", "--format=%H%x1f%B%x1e", f"{base}..HEAD", cwd=wt)
    commits = [tuple(entry.strip().split("\x1f", 1)) for entry in log.split("\x1e") if entry.strip()]
    merges = world.git("rev-list", "--merges", f"{base}..HEAD", cwd=wt).split()
    try:
        proof = json.load(open(os.path.join(wt, ".nzproof", "summary.json")))
    except (OSError, ValueError):
        proof = None
    tree = world.git("rev-parse", "HEAD^{tree}", cwd=wt).strip()
    dirty = bool(world.git("status", "--porcelain", cwd=wt).strip())
    refusals = plan.pr_refusals(files, commits, title, description, merges, proof, tree, dirty)
    if not shots_anyway:
        read = lambda path: world.git("show", f"HEAD:{path}", cwd=wt, check=False) or ""
        stray = shots.stray(files, shots.golden_names(read))
        refusals += [shots.refusal(stray)] if stray else []
    stacked = int(os.environ["NZ_BASE_PR"]) if os.environ.get("NZ_BASE_PR") else None
    body = plan.build_body(description, proof, stacked) if not refusals else ""
    refusals += [f"the proof output carries {what}. Remove it from the code or the test output" for what in plan.attribution(body)]
    if refusals:
        for r in refusals:
            say(f"REFUSED: {r}")
        return 2
    owner = _fork_owner(wt)
    remote = world.git("ls-remote", "origin", f"refs/heads/{branch}", cwd=wt).split()
    push = ["push", "--no-verify", "origin", f"HEAD:refs/heads/{branch}"]
    if remote:
        push.insert(2, f"--force-with-lease=refs/heads/{branch}:{remote[0]}")
    found = world.gh_json("pr", "list", "-R", UPSTREAM, "--head", branch, "--state", "open", "--json", "number,url") or []
    if dry_run:
        say(f"would run: git {' '.join(push)}")
        say(f"would {'update ' + found[0]['url'] if found else 'open a draft PR on ' + UPSTREAM + ' from ' + owner + ':' + branch + ' into main'}")
        say(f"title: {title}\n\n{body}")
        return 0
    world.git(*push, cwd=wt)
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir=config.private_tmpdir()) as f:
        f.write(body)
        f.flush()
        if found:
            world.gh("api", "-X", "PATCH", f"repos/{UPSTREAM}/pulls/{found[0]['number']}", "-f", f"title={title}", "-F", f"body=@{f.name}")
            url = found[0]["url"]
        else:
            url = world.gh("pr", "create", "-R", UPSTREAM, "--head", f"{owner}:{branch}", "--base", "main", "--draft",
                           "--title", title, "--body-file", f.name).strip().splitlines()[-1]
    say(url)
    say("Done. Free what you used: docker rm -f agentdb-$NZ_SLUG; rm -rf \"$CARGO_TARGET_DIR\"")
    return 0
