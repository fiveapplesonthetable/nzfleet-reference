"""`nzfleet prove`: runs the checks a change calls for, in the foreground, and keeps what they printed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

from . import areas, config, plan, procs, world
from .config import GRADLE_SLOT, SCCACHE

LIMIT = 60 * 60  # one check; a cold cargo build on a loaded machine is the longest thing here


def changed_files(wt: str, base: str) -> list:
    """What the work differs by from its base: commits, edits and new files alike."""
    tracked = world.git("diff", "--no-renames", "--name-only", base, cwd=wt).split("\n")
    new = world.git("ls-files", "--others", "--exclude-standard", cwd=wt).split("\n")
    return sorted({f for f in tracked + new if f})


def tree_of_files(wt: str) -> str:
    """The tree id of the files as they are on disk now, ignored files left out. A commit of exactly these has the same tree."""
    fd, index = tempfile.mkstemp(prefix="nzindex", dir=config.private_tmpdir())
    os.close(fd)
    try:
        shutil.copy(os.path.join(world.git("rev-parse", "--absolute-git-dir", cwd=wt).strip(), "index"), index)
    except OSError:
        pass
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        subprocess.run(["git", "add", "-A"], cwd=wt, env=env, check=True, capture_output=True)
        return subprocess.run(["git", "write-tree"], cwd=wt, env=env, check=True, capture_output=True, text=True).stdout.strip()
    finally:
        try:
            os.remove(index)
        except OSError:
            pass


def check_environment(check: plan.Check, inherited: dict, rust_cache: dict, gradle_slot: str) -> dict:
    """Adds only the cache and Android slot needed by a selected proof command."""
    env = dict(inherited)
    if check.cmd[0] == "cargo" or check.name == "conservative checks":
        env.update(rust_cache)
    if check.name == "conservative checks" and gradle_slot:
        env["NZELA_GRADLE_SLOT"] = gradle_slot
    return env


def run_check(argv: list, cwd: str, log: str, say, beat: float = procs.BEAT, limit: float = LIMIT,
              env: dict | None = None) -> int:
    """Runs one command with its output in `log`, saying where it is every `beat` seconds. Returns its exit code, or 124 on timeout."""
    with open(log, "w") as out:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=config.with_private_tmpdir(env), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError:
            out.write(f"error: {argv[0]} is not installed or not on PATH\n")
            return 127
    code = procs.wait_for(lambda: proc.poll() is None, lambda t: say(f"    {int(t // 60)} min so far; last line: {procs.last_line(log)}"),
                          limit=limit, beat=beat)
    if code == 3:
        procs.kill_group(proc.pid, now=True)
        proc.wait()
        with open(log, "a") as out:
            out.write(f"error: timed out after {int(limit // 60)} minutes\n")
        return 124
    return proc.returncode


def _wait_port(port: int, running) -> bool:
    """True once something answers on the port; False when `running()` says the server behind it is gone."""
    import socket
    while running():
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _preview(wt: str, say) -> None:
    """Serves the built console on the worktree's own port, once, for the checks that browse it."""
    port = int(os.environ["NZ_PORT"])
    if procs.start("preview", ["npx", "vite", "preview", "--port", str(port), "--strictPort", "--host", "127.0.0.1"], wt,
                   cwd=os.path.join(wt, "frontend/admin")) != "":
        return
    pid = procs._pid(wt, "preview")
    if pid is None or not _wait_port(port, lambda: procs.alive(pid)):
        say("the preview server exited before it answered on your port; see .nzrun/preview.log")


def prove(say=print) -> int:
    wt = procs.root()
    if not procs.load_env(wt) or "NZ_BASE_SHA" not in os.environ:
        say("this is not a worktree made by `nzfleet start`: there is no .nzenv here. Start one: nzfleet start <slug>")
        return 1
    base = os.environ["NZ_BASE_SHA"]
    files = changed_files(wt, base)
    tracked = world.git("ls-files", cwd=wt).split("\n")
    checks = plan.proof_plan(files, tracked, base)
    if any(c.needs == "db" for c in checks) and not os.environ.get("DATABASE_URL"):
        say("DATABASE_URL is not set. Run: cd " + wt + " && . ./.nzenv")
        return 1
    proof_dir = os.path.join(wt, ".nzproof")
    shutil.rmtree(proof_dir, ignore_errors=True)
    os.makedirs(proof_dir)
    say(f"{len(files)} files differ from the base; {len(checks)} checks: " + "; ".join(c.name for c in checks))
    results, began, failed = [], time.monotonic(), None
    rust_cache = areas.rust_cache_env(wt, SCCACHE)
    for i, check in enumerate(checks, 1):
        if failed:
            break
        if check.needs == "preview":
            _preview(wt, say)
        argv = ([GRADLE_SLOT] if check.slot and GRADLE_SLOT else []) + list(check.cmd)
        log = os.path.join(proof_dir, f"{i:02d}-{check.name.split(':')[0].replace(' ', '-')}.log")
        say(f"[{i}/{len(checks)}] {check.name}  ({' '.join(check.cmd)[:100]})")
        started = time.monotonic()
        env = check_environment(check, os.environ, rust_cache, GRADLE_SLOT)
        code = run_check(argv, os.path.join(wt, check.cwd), log, say, env=env)
        with open(log, errors="replace") as f:
            text = f.read()
        result = {"name": check.name, "cmd": list(check.cmd), "ok": code == 0, "seconds": int(time.monotonic() - started),
                  "log": log, "output": plan.tail(text), "error": "" if code == 0 else plan.first_error(text)}
        results.append(result)
        say(f"    {'passed' if code == 0 else 'FAILED'} in {result['seconds']} s")
        if code != 0:
            failed = result
    procs.kill("preview", wt)
    proof = {"head": world.git("rev-parse", "HEAD", cwd=wt).strip(), "tree": tree_of_files(wt), "base": base,
             "ok": failed is None, "checks": results, "finished": time.time()}
    with open(os.path.join(proof_dir, "summary.json"), "w") as f:
        json.dump(proof, f, indent=1)
    if failed:
        say(f"\nFAILED at {failed['name']}:\n{failed['error']}\n\nThe whole output: {failed['log']}")
        return 1
    say(f"\nPROVED in {int(time.monotonic() - began)} s. Commit anything prove changed (redrawn screenshots), then: nzfleet pr \"<title>\" -m \"<what and why>\"")
    return 0
