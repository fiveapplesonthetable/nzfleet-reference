"""`nzfleet prove`: runs the checks a change calls for, in the foreground, and keeps what they printed."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time

from . import areas, config, plan, procs, tmpdirs, world
from .config import GRADLE_SLOT, SCCACHE

LIMIT = 60 * 60  # one check; a cold cargo build on a loaded machine is the longest thing here


def capture_filter(observed: dict, area: str, base: str, head: str) -> str:
    """A renderer selection only when the visual plan identifies the current tree and complete scope."""
    if not isinstance(observed, dict) or observed.get("version") != 1 or observed.get("base") != base:
        return ""
    if not str(observed.get("head", "")).startswith(f"WORKTREE:{head}:"):
        return ""
    fallback = observed.get("full_fallback")
    if not isinstance(fallback, dict) or fallback.get(area) is not False:
        return ""
    collection = observed.get("renderer" if area == "console" else "selected")
    if not isinstance(collection, dict):
        return ""
    names = collection.get(area)
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names):
        return ""
    return ",".join(names)


def capture_selection(wt: str, base: str, area: str) -> str:
    """Uses the same visual planner as CI; an unavailable or uncertain plan draws everything."""
    planner = os.path.join(wt, "tools/nzfleet/shots/visual-plan.py")
    if not os.path.isfile(planner):
        return ""
    try:
        head = world.git("rev-parse", "HEAD", cwd=wt).strip()
        output = subprocess.run(["python3", planner, "--working-tree", "--base", base, "--head", "HEAD", "--json"],
                                cwd=wt, capture_output=True, text=True, check=True)
        return capture_filter(json.loads(output.stdout), area, base, head)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


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


def check_environment(check: plan.Check, inherited: dict, rust_cache: dict, gradle_slot: str, base: str, run_dir: str) -> dict:
    """Adds the base and only the cache and Android slot needed by a selected proof command, and keeps its Gradle
    daemons in a registry under `run_dir` so no other scope's daemon is ever reused."""
    # The compatibility, limit and console-wait checks diff against it and refuse to run without it.
    env = dict(inherited, NZELA_BASE=base)
    env["GRADLE_OPTS"] = tmpdirs.with_gradle_registry(inherited.get("GRADLE_OPTS", ""), f"{run_dir}/gradle-daemons")
    if check.cmd[0] == "cargo" or check.name == "conservative checks":
        env.update(rust_cache)
    if check.name == "conservative checks" and gradle_slot:
        env["NZELA_GRADLE_SLOT"] = gradle_slot
    if check.name in ("android screenshots", "console screenshots") and inherited.get("NZELA_SCREENSHOT_FULL") != "1":
        area, variable = ("android", "NZELA_SCREENSHOT_FILTER") if check.name.startswith("android") else ("console", "SHOT_NAMES")
        if not inherited.get(variable):
            selected = capture_selection(os.path.dirname(run_dir), base, area)
            if selected:
                env[variable] = selected
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


def shunt_plan(checks: list, wt: str, inherited: dict, rust_cache: dict, base: str, proof_dir: str) -> list:
    """Project commands and exclusive build resources for Shunt's proof runner."""
    result = []
    for check in checks:
        env = check_environment(check, inherited, rust_cache, GRADLE_SLOT, base, proof_dir)
        changes = [f"{key}={value}" for key, value in sorted(env.items()) if inherited.get(key) != value]
        argv = ([GRADLE_SLOT] if check.slot and GRADLE_SLOT else []) + list(check.cmd)
        command = "cd " + shlex.quote(os.path.join(wt, check.cwd)) + " && " + shlex.join(["env", *changes, *argv])
        if check.needs == "preview":
            package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            preview = shlex.join(["env", f"PYTHONPATH={package}", "python3", "-c",
                                  f"from nzfleet.prove import _preview; _preview({wt!r}, print)"])
            command = preview + " && " + command
        runner_area = check.cmd[1] if check.cmd[:1] == ("scripts/checks.sh",) and len(check.cmd) == 2 else ""
        area = ("android" if check.cwd == "frontend/android" or runner_area == "android" else
                "console" if check.cwd == "frontend/admin" or runner_area == "console" else
                "rust" if check.cmd[0] == "cargo" else "project")
        result.append({"name": check.name, "cmd": command, "area": area, "units": []})
    return result


def shunt_results(summary: dict, expected: list, checks: list, wt: str, proof_dir: str) -> list:
    """Imports only results for the complete commands the project actually requested."""
    actual = summary.get("checks", [])
    if len(actual) != len(expected) or any(
        got.get("name") != wanted["name"] or got.get("cmd") != wanted["cmd"]
        for got, wanted in zip(actual, expected)
    ):
        raise ValueError("Shunt did not return the complete requested proof plan")
    results = []
    for index, (got, check) in enumerate(zip(actual, checks)):
        source = os.path.join(wt, ".tachoproof", "logs", f"{index:03d}.log")
        log = os.path.join(proof_dir, f"{index + 1:02d}-shunt.log")
        shutil.copyfile(source, log)
        with open(log, errors="replace") as f:
            output = f.read()
        results.append({"name": check.name, "cmd": list(check.cmd), "ok": got["ok"],
                        "seconds": got["duration_ms"] // 1000, "log": log,
                        "output": plan.tail(output), "error": "" if got["ok"] else plan.first_error(output)})
    return results


def run_shunt(binary: str, checks: list, files: list, tracked: list, wt: str, base: str,
              proof_dir: str, rust_cache: dict, say) -> list:
    """Runs the project plan through Shunt and keeps nzfleet's publication format during migration."""
    expected = shunt_plan(checks, wt, os.environ, rust_cache, base, proof_dir)
    path = os.path.join(proof_dir, "shunt-plan.json")
    with open(path, "w") as f:
        json.dump(expected, f, indent=1)
    summary_path = os.path.join(wt, ".tachoproof", "summary.json")
    if os.path.exists(summary_path):
        os.remove(summary_path)
    outputs = {f for f in tracked + files if f.endswith(".png") and f.startswith(
        ("frontend/admin/screenshots/", "frontend/android/docs/screenshots/"))}
    outputs.add("frontend/admin/docs/comparison.html")
    argv = [binary, "prove", "--base", base, "--plan-file", path, "--keep-going", "--jobs",
            "1" if any(c.name == "conservative checks" for c in checks) else "3"]
    for area in ("project", "rust", "console", "android"):
        argv += ["--lane", f"{area}={area}"]
    for output in sorted(outputs):
        argv += ["--output", output]
    log = os.path.join(proof_dir, "shunt-runner.log")
    say("Shunt runs Rust, console and Android in separate resource lanes; shared build commands remain ordered.")
    try:
        runner_env = dict(os.environ, TACHO_HOME=os.path.join(proof_dir, "shunt-state"))
        runner_env.pop("TACHO_RUNTIME", None)
        code = run_check(argv, wt, log, say, env=runner_env)
        with open(summary_path) as f:
            summary = json.load(f)
        if summary.get("base") != base or summary.get("head") != world.git("rev-parse", "HEAD", cwd=wt).strip():
            raise ValueError("Shunt returned proof for a different base or head")
        if summary.get("tree") != tree_of_files(wt):
            raise ValueError("Shunt returned proof for a different source tree")
        results = shunt_results(summary, expected, checks, wt, proof_dir)
        if (code != 0 or not summary.get("ok")) and all(result["ok"] for result in results):
            raise ValueError("Shunt rejected the proof; see " + log)
        return results
    except (OSError, ValueError, KeyError, TypeError) as cause:
        return [{"name": "Shunt proof", "cmd": [binary, "prove"], "ok": False, "seconds": 0,
                 "log": log, "output": "", "error": str(cause)}]
    finally:
        procs.kill("preview", wt)


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
    runner = os.environ.get("NZFLEET_PROOF_RUNNER")
    if runner:
        results = run_shunt(runner, checks, files, tracked, wt, base, proof_dir, rust_cache, say)
        failed = next((result for result in results if not result["ok"]), None)
    for i, check in enumerate([] if runner else checks, 1):
        if failed:
            break
        if check.needs == "preview":
            _preview(wt, say)
        argv = ([GRADLE_SLOT] if check.slot and GRADLE_SLOT else []) + list(check.cmd)
        log = os.path.join(proof_dir, f"{i:02d}-{check.name.split(':')[0].replace(' ', '-')}.log")
        say(f"[{i}/{len(checks)}] {check.name}  ({' '.join(check.cmd)[:100]})")
        started = time.monotonic()
        env = check_environment(check, os.environ, rust_cache, GRADLE_SLOT, base, proof_dir)
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
