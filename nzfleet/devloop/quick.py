"""Runs only the tests a local edit can affect, against servers that stay warm between runs.

  nzfleet-quick up|down|status     the resident Postgres, the Gradle daemon and the warm cargo target
  nzfleet-quick [run] [--plan]   map the changed files to tests and run them (--plan only prints)

Changed files are the working tree against HEAD, or the last commit when the tree is clean;
QUICK_SINCE=<ref> compares against that ref instead. Presubmit still runs what quick leaves out.
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from . import plan

ROOT = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
SLUG = re.sub(r"[^a-z0-9-]", "-", ROOT.name.lower())
STATE_DIR = pathlib.Path(os.environ.get("QUICK_DIR", f"/dev/shm/nzquick/{SLUG}"))
STATE = STATE_DIR / "state.json"
CARGO = os.environ.get("NZELA_CARGO", "cargo +1.98").split()
PSQL = shutil.which("psql") or "/srv/fleet/bin/psql"
POSTGRES_IMAGE = "postgres:16"


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=kw.pop("cwd", ROOT), text=True, **kw)


def output(cmd, **kw):
    return run(cmd, capture_output=True, **kw).stdout


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def migration_files():
    files = sorted(ROOT.glob("crates/*/migrations/*.sql"), key=lambda p: p.name)
    return files


def migration_hash():
    digest = hashlib.sha256()
    for path in migration_files():
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def container_running(name):
    return output(["docker", "inspect", "-f", "{{.State.Running}}", name]).strip() == "true"


def database_url(port):
    return f"postgres://postgres:nzela@127.0.0.1:{port}/nzela"


def up_postgres(state):
    name = f"nzquick-{SLUG}"
    wanted = migration_hash()
    if state.get("postgres") == wanted and container_running(name):
        return
    run(["docker", "rm", "-f", name], capture_output=True)
    port = output([sys.executable, "-c", "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])"]).strip()
    run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5432", "--tmpfs", "/var/lib/postgresql/data:rw,size=4g",
         "-e", "POSTGRES_PASSWORD=nzela", POSTGRES_IMAGE, "-c", "fsync=off", "-c", "synchronous_commit=off",
         "-c", "full_page_writes=off", "-c", "max_connections=300"], check=True, capture_output=True)
    while run(["docker", "exec", name, "pg_isready", "-q", "-U", "postgres"], capture_output=True).returncode:
        if not container_running(name):
            raise SystemExit("quick: postgres stopped before it was ready")
        time.sleep(0.25)
    env = {**os.environ, "PGOPTIONS": "-c client_min_messages=warning"}
    # The server answers pg_isready once during initdb and again after the restart that follows it.
    postgres = f"postgres://postgres:nzela@127.0.0.1:{port}/postgres"
    while run([PSQL, postgres, "-qc", "select 1"], capture_output=True, env=env).returncode:
        time.sleep(0.25)
    run([PSQL, postgres, "-qc", "create database nzela"], check=True, env=env, capture_output=True)
    url = database_url(port)
    for path in migration_files():
        run([PSQL, url, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)], check=True, env=env, capture_output=True)
    run(["tools/nzfleet/bin/nzfleet-migration", "template", url], check=True, env={**env, "PATH": f"{os.path.dirname(PSQL)}:{os.environ['PATH']}"})
    state.update(postgres=wanted, database_url=url)


def up_gradle():
    android = ROOT / "frontend/android"
    if not (android / "gradlew").exists():
        return
    run(["/srv/fleet/bin/gradle-slot", "./gradlew", "--daemon", "-q", "help"], cwd=android, env=gradle_env(), check=True, capture_output=True)


def gradle_env():
    return {**os.environ, "ANDROID_HOME": os.environ.get("ANDROID_HOME", "/home/user/android-sdk")}


def cargo_env(state):
    target = STATE_DIR / "target"
    return {
        **os.environ,
        "DATABASE_URL": state["database_url"],
        "CARGO_TARGET_DIR": str(target),
        "CARGO_INCREMENTAL": "1",
        "CARGO_PROFILE_DEV_DEBUG": "0",
        "CARGO_PROFILE_TEST_DEBUG": "0",
        "RUST_TEST_THREADS": os.environ.get("RUST_TEST_THREADS", "8"),
    }


def up_console():
    admin = ROOT / "frontend/admin"
    if not (admin / "node_modules").exists():
        run(["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"], cwd=admin, check=True)


def up():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    up_postgres(state)
    STATE.write_text(json.dumps(state))
    up_console()
    up_gradle()
    started = time.monotonic()
    run([*CARGO, "test", "--locked", "--workspace", "--no-run", "-q"], env=cargo_env(state), check=True)
    print(f"quick: servers up; cargo target warm in {time.monotonic() - started:.1f}s", file=sys.stderr)


def down():
    run(["docker", "rm", "-f", f"nzquick-{SLUG}"], capture_output=True)
    run(["./gradlew", "--stop"], cwd=ROOT / "frontend/android", capture_output=True, env=gradle_env())
    shutil.rmtree(STATE_DIR, ignore_errors=True)


def status():
    state = load_state()
    name = f"nzquick-{SLUG}"
    print(f"postgres  {'up' if container_running(name) else 'down'}  {state.get('database_url', '-')}")
    print(f"cargo     {'warm' if (STATE_DIR / 'target').exists() else 'cold'}  {STATE_DIR / 'target'}")
    daemons = output(["./gradlew", "--status"], cwd=ROOT / "frontend/android", env=gradle_env()).strip().splitlines()[1:3]
    print("gradle    " + (" | ".join(line.strip() for line in daemons) if daemons else "no daemon"))


def changed_files():
    since = os.environ.get("QUICK_SINCE")
    if since:
        base = since
    else:
        base = "HEAD" if output(["git", "status", "--porcelain", "--untracked-files=no"]).strip() else "HEAD^"
    tracked = output(["git", "diff", "--no-renames", "--name-only", base]).split("\n")
    untracked = output(["git", "ls-files", "-o", "--exclude-standard"]).split("\n")
    return sorted({p for p in tracked + untracked if p})


def read_tree(prefix, suffixes):
    files = {}
    for path in output(["git", "ls-files", "-co", "--exclude-standard", prefix]).split("\n"):
        full = ROOT / path
        if path.endswith(suffixes) and full.is_file():
            files[path] = full.read_text(errors="replace")
    return files


def cargo_packages():
    meta = json.loads(output([*CARGO, "metadata", "--no-deps", "--format-version", "1", "--locked"]))
    members = {p["name"] for p in meta["packages"]}
    dependents = {}
    for pkg in meta["packages"]:
        for dep in pkg["dependencies"]:
            if dep["name"] in members and dep.get("path"):
                dependents.setdefault(dep["name"], set()).add(pkg["name"])
    closed = {}
    for crate in members:
        seen, pending = set(), [crate]
        while pending:
            for parent in dependents.get(pending.pop(), ()):
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        closed[crate] = seen
    return closed


def make_plan(changed):
    rust_sources = {p: t for p, t in read_tree("crates", (".rs",)).items() if re.match(r"crates/[^/]+/(src|tests)/", p)}
    return {
        "changed": changed,
        "rust": plan.rust_plan(changed, cargo_packages(), rust_sources, None if os.environ.get("DEPS") else 1),
        "android": plan.android_plan(changed, read_tree("frontend/android/app/src", (".kt",)), None if os.environ.get("DEPS") else 0),
        "console": plan.console_plan(changed, read_tree("frontend/admin", (".ts", ".tsx", ".mjs")), console_tests()),
        "elsewhere": plan.uncovered(changed),
    }


def console_tests():
    return {str(p.relative_to(ROOT)) for p in (ROOT / "frontend/admin/tests").glob("*.test.mjs")}


def timed(label, cmd, **kw):
    started = time.monotonic()
    code = run(cmd, **kw).returncode
    print(f"quick: {label}: {'ok' if code == 0 else 'FAILED'} in {time.monotonic() - started:.1f}s", file=sys.stderr)
    return code


def grouped(pairs):
    out = {}
    for pair in pairs:
        crate, name = pair.split(":", 1)
        out.setdefault(crate, []).append(name)
    return out


def run_rust(selection, state):
    env = cargo_env(state)
    cargo_test = [*CARGO, "test", "--locked", "--no-fail-fast"]
    if selection["mode"] == "all":
        return timed("rust (workspace)", [*cargo_test, "--workspace"], env=env)
    failed = 0
    whole = selection["whole"] + (selection["downstream"] if os.environ.get("DEPS") else [])
    for crate in dict.fromkeys(whole):
        failed |= timed(f"rust {crate}", [*cargo_test, "-p", crate, "--lib", "--bins", "--tests"], env=env)
    by_crate = grouped(selection["modules"])
    if by_crate:
        packages = [arg for crate in by_crate for arg in ("-p", crate)]
        filters = sorted({f"{m}::" for modules in by_crate.values() for m in modules})
        failed |= timed(f"rust {', '.join(by_crate)} lib", [*cargo_test, *packages, "--lib", "--", *filters], env=env)
    for crate, names in grouped(selection["tests"]).items():
        binaries = [arg for t in names for arg in ("--test", t)]
        failed |= timed(f"rust {crate} tests {', '.join(names)}", [*cargo_test, "-p", crate, *binaries], env=env)
    return failed


def run_android(selection):
    android = ROOT / "frontend/android"
    task = [":app:testDebugUnitTest"]
    if selection["mode"] == "select":
        task += [arg for c in selection["classes"] for arg in ("--tests", c)]
        if not selection["classes"]:
            return 0
    return timed("android", ["/srv/fleet/bin/gradle-slot", "./gradlew", "--daemon", "-q", *task], cwd=android, env=gradle_env())


def run_console(selection):
    tests = [t.removeprefix("frontend/admin/") for t in selection.get("tests", [])]
    if selection["mode"] == "all":
        tests = sorted(t.name for t in (ROOT / "frontend/admin/tests").glob("*.test.mjs"))
        tests = [f"tests/{t}" for t in tests]
    if not tests:
        return 0
    return timed("console", ["node", "--experimental-strip-types", "--test", *tests], cwd=ROOT / "frontend/admin")


def execute(show_only):
    changed = changed_files()
    chosen = make_plan(changed)
    print(json.dumps(chosen, indent=2))
    if show_only:
        return 0
    state = load_state()
    if not state:
        up()
        state = load_state()
    failed = 0
    if chosen["rust"]["mode"] != "none":
        failed |= run_rust(chosen["rust"], state)
    if chosen["android"]["mode"] != "none":
        failed |= run_android(chosen["android"])
    if chosen["console"]["mode"] != "none":
        failed |= run_console(chosen["console"])
    left = chosen["elsewhere"]
    if left:
        print(f"quick: not covered here, presubmit decides: {', '.join(left[:6])}{' ...' if len(left) > 6 else ''}", file=sys.stderr)
    return 1 if failed else 0


def main(argv):
    command = argv[1] if len(argv) > 1 else "run"
    if command == "up":
        up()
    elif command == "down":
        down()
    elif command == "status":
        status()
    elif command in ("run", "--plan"):
        return execute(command == "--plan" or "--plan" in argv[2:])
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
