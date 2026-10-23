"""`nzfleet start`: a worktree at a pinned base with its own branch, ports, database and node_modules."""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess

from . import config, db, plan, procs, state, world
from .config import ANDROID_HOME, GIT_EMAIL, GIT_NAME, HOME, TARGETS, TOOLCHAIN, WORK

ADMIN = "frontend/admin"


def _busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _node_cache(wt: str, say) -> int:
    """Links node_modules from a cache keyed by the lock file, installing it once. 0 when linked, 3 when still installing."""
    lock = os.path.join(wt, ADMIN, "package-lock.json")
    target = os.path.join(wt, ADMIN, "node_modules")
    if not os.path.exists(lock) or os.path.isdir(target):
        return 0
    key = plan.lock_key(open(lock, "rb").read())
    cache = os.path.join(HOME, "cache", "node", key)
    ready, pidfile, log = os.path.join(cache, "node_modules"), os.path.join(cache, "install.pid"), os.path.join(cache, "install.log")
    os.makedirs(cache, exist_ok=True)

    def installing() -> bool:
        try:
            return procs.alive(int(open(pidfile).read()))
        except (OSError, ValueError):
            return False

    with open(os.path.join(cache, "lock"), "w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if plan.node_cache_state(os.path.isdir(ready), installing()) == "absent":
            say(f"installing node_modules once for this lock file ({key}); every later start links it")
            script = (f"set -e; rm -rf build; mkdir build; cp '{wt}/{ADMIN}/package.json' '{wt}/{ADMIN}/package-lock.json' build/; "
                      "cd build; npm ci --no-audit --no-fund; mv node_modules ..")
            with open(log, "w") as output:
                proc = subprocess.Popen(["bash", "-c", script], cwd=cache, env=config.with_private_tmpdir(), stdout=output,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            with open(pidfile, "w") as f:
                f.write(str(proc.pid))
    code = procs.wait_for(lambda: not os.path.isdir(ready) and installing(),
                          lambda t: say(f"npm ci still running after {int(t // 60)} min"))
    if code == 3:
        say("npm ci is still running. Run the same `nzfleet start` command again to carry on")
        return 3
    if not os.path.isdir(ready):
        try:
            why = plan.first_error(open(log, errors="replace").read())
        except OSError:
            why = "no log"
        say(f"npm ci failed: {why}\nDelete {cache} and run the same `nzfleet start` command again")
        return 1
    linked = subprocess.run(["cp", "-al", ready, target], capture_output=True)
    if linked.returncode != 0:  # the cache and the worktree are on different disks
        subprocess.run(["rm", "-rf", target])
        subprocess.run(["cp", "-a", "--reflink=auto", ready, target], check=True)
    say("node_modules linked from the cache")
    return 0


def claim(slug: str, key: str) -> None:
    """Records which task works in the worktree, so a run that dies can be resumed in it."""
    if key:
        with state.change() as s:
            s["worktrees"][slug]["agent"] = key


def start(slug: str, base: str, say=print) -> int:
    problem = plan.slug_problem(slug)
    if problem:
        say(problem)
        return 1
    pr = int(base.lstrip("#")) if base.lstrip("#").isdigit() else None
    if base != "main" and pr is None:
        say("--base is `main` or a PR number, e.g. --base 816")
        return 1
    path = os.path.join(WORK, slug)
    known = state.read()["worktrees"].get(slug)
    if os.path.isdir(path) and known is None:
        say(f"{path} exists and was not made by `nzfleet start`. Choose another slug")
        return 1
    if known is None:
        if world.git_ok("show-ref", "--verify", "--quiet", f"refs/heads/{slug}"):
            say(f"a branch named {slug} already exists. Choose another slug, or continue it with: cd {path} && . ./.nzenv")
            return 1
        try:
            world.fetch([pr] if pr else [])
            sha = world.head_of(pr) if pr else world.main_sha()
        except world.Failed as e:
            say(f"could not fetch the base: {e}. Is the network up (git ls-remote upstream)?")
            return 1
        say(f"base {sha[:10]} ({'PR #' + str(pr) if pr else 'upstream/main'})")
        world.git("worktree", "add", "-q", "-b", slug, path, sha)
        with state.change() as s:
            taken = {w["port"] for w in s["worktrees"].values() if os.path.isdir(w["path"])}
            port = plan.allocate_ports(taken, _busy)
            if port is None:
                say("every port block is in use. Remove a finished worktree: git worktree remove <path>")
                return 1
            s["worktrees"][slug] = {"path": path, "base": sha, "base_pr": pr, "port": port}
        known = state.read()["worktrees"][slug]
    claim(slug, os.environ.get("NZFLEET_AGENT_KEY", ""))
    port, sha = known["port"], known["base"]
    exclude = world.git("rev-parse", "--git-path", "info/exclude", cwd=path).strip()
    exclude = exclude if os.path.isabs(exclude) else os.path.join(path, exclude)
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    have = open(exclude).read() if os.path.exists(exclude) else ""
    with open(exclude, "a") as f:
        f.write("".join(f"{n}\n" for n in (".nzenv", ".nzproof", ".nzrun") if n not in have.split()))
    try:
        say("starting the database and applying every migration")
        url = db.up(slug, path, port + 1)
    except world.Failed as e:
        say(str(e))
        return 1
    code = _node_cache(path, say)
    if code:
        return code
    values = {
        "NZ_SLUG": slug, "NZ_WORKTREE": path, "NZ_BASE_SHA": sha, "NZ_BASE_PR": known["base_pr"] or "",
        "NZ_PORT": port, "NZ_PORT_LAST": port + plan.PORT_SIZE - 1,
        "CONSOLE_URL": f"http://127.0.0.1:{port}", "SHOT_URL": f"http://127.0.0.1:{port}",
        "DATABASE_URL": url, "CARGO_TARGET_DIR": os.path.join(TARGETS, slug),
        "CARGO_PROFILE_DEV_DEBUG": 0, "CARGO_PROFILE_TEST_DEBUG": 0, "CARGO_INCREMENTAL": 0,
    }
    if ANDROID_HOME:
        values["ANDROID_HOME"] = ANDROID_HOME
    for key, value in TOOLCHAIN.items():
        values[key] = plan.merge_path(value, os.environ.get("PATH", "")) if key == "PATH" else value
    if GIT_NAME:
        values.update(GIT_AUTHOR_NAME=GIT_NAME, GIT_COMMITTER_NAME=GIT_NAME, GIT_AUTHOR_EMAIL=GIT_EMAIL, GIT_COMMITTER_EMAIL=GIT_EMAIL)
    with open(os.path.join(path, ".nzenv"), "w") as f:
        f.write(plan.env_file(values))
    say(f"ports {port}..{port + plan.PORT_SIZE - 1} are yours; the database is on {port + 1}")
    say("")
    say(f"cd {path} && . ./.nzenv")
    return 0
