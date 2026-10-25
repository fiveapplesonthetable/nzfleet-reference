#!/bin/bash
# Frees disk on the runner pools' own Docker daemons. Run as root from cron:
#
#   7 * * * *    tools/nzfleet/runner/runner-prune.sh scheduled: routine and low-disk cleanup
#
# Image, container and builder pruning waits for an idle pool: a prune mid-build
# removes layers being exported. Owned, unreferenced run volumes can be checked
# independently even when unrelated containers are running.
set -uo pipefail

case $#:${1:-} in
    0:|1:hourly) mode=hourly ;;
    1:scheduled) mode=scheduled ;;
    1:check) mode=check ;;
    1:plan) mode=plan ;;
    1:--help|1:-h)
        echo 'usage: runner-prune.sh [hourly|scheduled|check|plan]'
        exit 0 ;;
    *) echo 'usage: runner-prune.sh [hourly|scheduled|check|plan]' >&2; exit 2 ;;
esac

pools=${RUNNER_PRUNE_POOLS:-"nzela-ci:1001 nzela-pr:1002"}
free=${RUNNER_PRUNE_FREE_GB:-$(df --output=avail -BG / | tail -1 | tr -dc 0-9)}
low=40 critical=15

docker_as() {
    local name=$1 id=$2
    shift 2
    if [ -n "${RUNNER_PRUNE_DOCKER:-}" ]; then
        "$RUNNER_PRUNE_DOCKER" "$name" "$@"
    else
        runuser -u "$name" -- env XDG_RUNTIME_DIR="/run/user/$id" \
            DOCKER_HOST="unix:///run/user/$id/docker.sock" docker "$@"
    fi
}

# A job holds the pool from the runner's worker starting until it exits; a
# container still running covers a build the worker has already left.
busy() {
    local name=$1 id=$2
    if [ -n "${RUNNER_PRUNE_BUSY:-}" ]; then
        "$RUNNER_PRUNE_BUSY" "$name"
        return
    fi
    pgrep -u "$name" -f Runner.Worker >/dev/null && return 0
    [ -n "$(docker_as "$name" "$id" ps -q 2>/dev/null)" ]
}

if [ "$mode" = scheduled ]; then
    mode=hourly
    if [ "$free" -lt "$low" ]; then
        mode=low
        [ "$free" -lt "$critical" ] && mode=critical
    fi
elif [ "$mode" = check ]; then
    [ "$free" -ge "$low" ] && exit 0
    mode=low
    [ "$free" -lt "$critical" ] && mode=critical
fi

# The embedded planner keeps the installed root script self-contained. GitHub
# run status and Docker references must both be known before removing a volume.
warm_volumes() {
    local name=$1 id=$2
    RUNNER_PRUNE_POOL="$name" RUNNER_PRUNE_UID="$id" RUNNER_PRUNE_MODE="$mode" python3 - <<'PY'
import datetime
import json
import os
import pwd
import re
import selectors
import signal
import subprocess
import sys
import time

pool = os.environ["RUNNER_PRUNE_POOL"]
uid = os.environ["RUNNER_PRUNE_UID"]
mode = os.environ["RUNNER_PRUNE_MODE"]
# Both pools look runs up in nzela-org/nzela: a fork's pull request runs in the base repository, so the
# runner's own registration (the fork, for the pull-request pool) names the wrong one.
repositories = dict(entry.split("=", 1) for entry in os.environ.get(
    "RUNNER_PRUNE_REPOS", "nzela-ci=nzela-org/nzela nzela-pr=nzela-org/nzela").split() if "=" in entry)
repository = repositories.get(pool)
if not repository or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
    print(f"runner-prune: {pool}: run-volume owner is not configured; kept", file=sys.stderr)
    sys.exit(1)
docker = ([os.environ["RUNNER_PRUNE_DOCKER"], pool] if os.environ.get("RUNNER_PRUNE_DOCKER") else
          ["runuser", "-u", pool, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
           f"DOCKER_HOST=unix:///run/user/{uid}/docker.sock", "docker"])
gh = os.environ.get("RUNNER_PRUNE_GH", "gh")
now = datetime.datetime.now(datetime.timezone.utc)
deadline = time.monotonic() + 120
MAX_INVENTORY_BYTES = 2_000_000
MAX_STDERR_BYTES = 65_536
MAX_VOLUMES = 500
MAX_API_CALLS = 400
FACT_SHARE = 0.4
CALL_MARGIN = 35
started = time.monotonic()
api_calls = 0

def gh_identity():
    user = os.environ.get("RUNNER_PRUNE_GH_USER", "")
    if not user and os.path.exists("/etc/nzela-runner-prune.conf"):
        path = "/etc/nzela-runner-prune.conf"
        info = os.stat(path, follow_symlinks=False)
        if not os.path.isfile(path) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("GitHub identity config is not root-owned and private")
        with open(path, encoding="utf-8") as source:
            lines = source.read(256).splitlines()
        if len(lines) != 1 or not lines[0].startswith("RUNNER_PRUNE_GH_USER="):
            raise ValueError("GitHub identity config is malformed")
        user = lines[0].split("=", 1)[1]
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("GitHub read-only identity is not configured")
    account = pwd.getpwnam(user)
    if account.pw_uid == 0 or not os.path.isabs(account.pw_dir):
        raise ValueError("GitHub identity must be an unprivileged local account")
    home = account.pw_dir
    if os.stat(home).st_uid != account.pw_uid:
        raise ValueError("GitHub identity home has the wrong owner")
    env = {**os.environ, "HOME": home, "GH_CONFIG_DIR": os.path.join(home, ".config", "gh")}
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    command = [gh] if os.getuid() == account.pw_uid else ["runuser", "-u", user, "--", gh]
    return command, env

try:
    gh_command, gh_env = gh_identity()
except (KeyError, ValueError, OSError) as error:
    print(f"runner-prune: {pool}: GitHub identity unavailable: {error}", file=sys.stderr)
    sys.exit(1)

def call(argv, max_bytes=65536, env=None, label=None):
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        raise TimeoutError("120-second cleanup budget exhausted")
    end = min(started + 30, deadline)
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    completed = False
    output = bytearray()
    errors = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, (output, max_bytes, "output"))
            selector.register(process.stderr, selectors.EVENT_READ, (errors, MAX_STDERR_BYTES, "error output"))
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{argv[0]} exceeded cleanup deadline")
                for key, _ in selector.select(remaining):
                    captured, limit, stream = key.data
                    chunk = os.read(key.fd, min(65536, limit + 1 - len(captured)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        captured.extend(chunk)
                        if len(captured) > limit:
                            raise ValueError(f"{argv[0]} {stream} exceeds {limit} bytes")
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{argv[0]} exceeded cleanup deadline")
        code = process.wait(timeout=remaining)
        if code:
            detail = errors.decode("utf-8", "replace").strip().splitlines()
            raise ValueError(f"{label or argv[0]} returned {code}" + (f": {detail[-1][:200]}" if detail else ""))
        try:
            decoded = output.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{argv[0]} returned invalid UTF-8") from None
        completed = True
        return decoded
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.stdout.close()
        process.stderr.close()
        process.wait()

def docker_json(*args):
    return json.loads(call([*docker, *args], MAX_INVENTORY_BYTES))

def run_fact(run_id):
    global api_calls
    api_calls += 1
    if api_calls > MAX_API_CALLS:
        raise ValueError("GitHub API call budget exhausted")
    path = f"repos/{repository}/actions/runs/{run_id}"
    data = json.loads(call([*gh_command, "api", path], env=gh_env, label=f"gh api {path}"))
    if (data.get("id") != int(run_id) or not isinstance(data.get("repository"), dict) or
            data["repository"].get("full_name") != repository or
            data.get("status") not in ("completed", "queued", "in_progress", "waiting", "pending", "requested") or
            data.get("status") == "completed" and data.get("conclusion") not in
            ("success", "failure", "cancelled", "skipped", "timed_out", "action_required", "neutral", "stale", "startup_failure")):
        raise ValueError("GitHub returned an unknown run state")
    return data

def inspecting():
    """True while the status lookups may still use their share of the pass; removals keep the rest."""
    return api_calls < MAX_API_CALLS // 2 and time.monotonic() - started < FACT_SHARE * (deadline - started)

def removing():
    """True while one more removal fits the pass; the next pass carries on from where this one stopped."""
    return api_calls < MAX_API_CALLS and deadline - time.monotonic() > CALL_MARGIN

def bytes_of(size):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB)", size)
    if not match:
        return None
    return int(float(match.group(1)) * (1000 ** ("B", "kB", "MB", "GB", "TB").index(match.group(2))))

def retention(owned, facts, now):
    updated = {}
    for run_id, fact in facts.items():
        at = datetime.datetime.fromisoformat(fact["updated_at"].replace("Z", "+00:00"))
        if at.tzinfo is None or at > now:
            raise ValueError("GitHub run time is malformed")
        updated[run_id] = at
    completed = sorted((int(run_id) for run_id, fact in facts.items() if fact["status"] == "completed"),
                       key=lambda run_id: (updated[str(run_id)], run_id), reverse=True)
    newest = set(completed[:2])
    green = next((run_id for run_id in completed if facts[str(run_id)].get("conclusion") == "success"), None)
    if green is not None:
        newest.add(green)
    rows = []
    for name, (run_id, links, size) in sorted(owned.items()):
        fact = facts[run_id]
        if links:
            reason = f"kept: {links} Docker container reference(s)"
            remove = False
        elif fact["status"] != "completed":
            reason = f"kept: run {run_id} is {fact['status']}"
            remove = False
        elif int(run_id) in newest:
            reason = "kept: recent completed run or latest successful cache"
            remove = False
        elif now - updated[run_id] < datetime.timedelta(hours=2):
            reason = "kept: completed less than two hours ago"
            remove = False
        else:
            reason = "remove: completed run beyond two retained runs and two hours"
            remove = True
        rows.append((name, reason, size, remove))
    return rows

try:
    usage = docker_json("system", "df", "-v", "--format", "json")
    volumes = usage["Volumes"]
    if not isinstance(volumes, list):
        raise ValueError("Docker volume inventory is malformed")
    if len(volumes) > MAX_VOLUMES:
        raise ValueError(f"Docker volume inventory exceeds {MAX_VOLUMES} entries")
    owned = {}
    for volume in volumes:
        name = volume["Name"]
        match = re.fullmatch(r"nzela-warm-([1-9][0-9]*)", name)
        if match:
            if name in owned or volume.get("Driver") != "local" or volume.get("Scope") != "local":
                raise ValueError("Docker volume ownership is ambiguous")
            links = volume["Links"]
            if not isinstance(links, str) or not links.isdigit():
                raise ValueError("Docker volume references are malformed")
            owned[name] = (match.group(1), int(links), bytes_of(volume["Size"]))
            if owned[name][2] is None:
                raise ValueError("Docker volume size is unknown")
    if not owned:
        print(f"runner-prune: {pool}: no run volumes")
        sys.exit(0)
    facts = {}
    for run_id in sorted({row[0] for row in owned.values()}, key=int, reverse=True):
        if not inspecting():
            break
        facts[run_id] = run_fact(run_id)
    uninspected = {name: row for name, row in owned.items() if row[0] not in facts}
    rows = retention({name: row for name, row in owned.items() if row[0] in facts}, facts, now)
    rows += [(name, "kept: run not inspected within this pass; the next pass continues", row[2], False)
             for name, row in sorted(uninspected.items())]
    victims = sorted((name for name, _, _, remove in rows if remove), key=lambda name: int(owned[name][0]))
    expected = sum(size for _, _, size, remove in rows if remove)
    for name, reason, size, _ in rows:
        print(f"runner-prune: {pool}: {name}: {reason}; expected bytes {size}")
    print(f"runner-prune: {pool}: {len(victims)} volume(s) eligible; expected bytes {expected}")
    if mode != "plan":
        for position, name in enumerate(victims):
            if not removing():
                print(f"runner-prune: {pool}: pass budget spent; {len(victims) - position} volume(s) left for the next pass")
                break
            run_id = owned[name][0]
            if run_fact(run_id) != facts[run_id]:
                raise ValueError(f"GitHub run {run_id} changed during cleanup")
            current = docker_json("system", "df", "-v", "--format", "json")
            current_volumes = current.get("Volumes")
            if not isinstance(current_volumes, list) or len(current_volumes) > MAX_VOLUMES:
                raise ValueError("Docker recheck inventory is malformed or over budget")
            matching = [volume for volume in current_volumes if volume.get("Name") == name]
            if (len(matching) != 1 or matching[0].get("Driver") != "local" or
                    matching[0].get("Scope") != "local" or matching[0].get("Links") != "0"):
                raise ValueError(f"Docker references for {name} changed; retained")
            try:
                call([*docker, "volume", "rm", name])
            except ValueError as error:
                raise ValueError(f"Docker refused {name}; retained: {error}") from error
            print(f"runner-prune: {pool}: removed {name}")
except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
    print(f"runner-prune: {pool}: volume cleanup skipped: {error}", file=sys.stderr)
    sys.exit(1)
PY
}

# Leftovers of finished jobs in the account's temporary directory. The pool is idle here, so nothing running
# holds them; four hours matches the tidy pass over the agents' directories.
sweep_tmp() {
    local dir=${RUNNER_PRUNE_TMP:-/srv/fleet/tmp}/$1
    [ -d "$dir" ] && [ ! -L "$dir" ] || return 0
    find "$dir" -mindepth 2 -depth -mmin +240 -delete 2>/dev/null
    return 0
}

failed=0
for pool in $pools; do
    name=${pool%%:*} id=${pool##*:}
    if ! warm_volumes "$name" "$id"; then
        echo "runner-prune: $name volume cleanup failed" >&2
        failed=1
    fi
    [ "$mode" = plan ] && continue
    if { [ "$mode" = hourly ] || [ "$mode" = low ]; } && busy "$name" "$id"; then
        echo "runner-prune: $name is running a job; image, builder and container caches left alone ($mode, ${free} GB free)"
        continue
    fi
    sweep_tmp "$name"
    d() {
        if ! docker_as "$name" "$id" "$@" >/dev/null 2>&1; then
            echo "runner-prune: $name: Docker $* failed" >&2
            failed=1
        fi
    }
    case $mode in
        hourly)
            d container prune -f --filter until=2h
            d image prune -f --filter until=2h
            d builder prune -af --filter until=2h
            ;;
        low | critical)
            d container prune -f
            d image prune -af
            d builder prune -af
            ;;
    esac
    echo "runner-prune: $name image, builder and container pass finished ($mode, ${free} GB free)"
done
exit "$failed"
