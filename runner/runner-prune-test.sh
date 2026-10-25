#!/bin/bash
# What runner-prune.sh promises, against a stand-in docker that records each call.

set -uo pipefail

if [ "$(id -u)" -eq 0 ]; then
    fixture_user=${RUNNER_PRUNE_TEST_USER:-node}
    if ! id "$fixture_user" >/dev/null 2>&1 || [ "$(id -u "$fixture_user")" -eq 0 ]; then
        echo "runner-prune fixture needs an unprivileged local account" >&2
        exit 1
    fi
    exec runuser -u "$fixture_user" -- env RUNNER_PRUNE_TEST_USER="$fixture_user" bash "$0" "$@"
fi

here="$(cd -- "$(dirname -- "$0")" && pwd)"
prune_script=${RUNNER_PRUNE_SCRIPT:-"$here/runner-prune.sh"}
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }

cat > "$work/docker" <<'STUB'
#!/bin/bash
echo "$*" >> "$CALLS"
if [ "$2 $3" = "system df" ]; then
    if [ -n "${FLOOD_STREAM:-}" ]; then
        python3 - "$FLOOD_STREAM" "$FLOOD_MARKER" <<'PY'
import os
import sys
stream = 1 if sys.argv[1] == "stdout" else 2
os.write(stream, b"private-sentinel")
for _ in range(1024):
    os.write(stream, b"x" * 4096)
if stream == 2:
    print('{"Volumes":[]}')
with open(sys.argv[2], "w", encoding="utf-8") as marker:
    marker.write("producer finished")
PY
    elif [ -n "${REF_ON_RECHECK:-}" ] && [ "$(grep -c '^ci system df ' "$CALLS")" -ge 2 ]; then
        python3 - "$VOLUME_JSON" "$REF_ON_RECHECK" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    data = json.load(source)
for volume in data["Volumes"]:
    if volume["Name"] == sys.argv[2]:
        volume["Links"] = "1"
print(json.dumps(data))
PY
    else
        cat "$VOLUME_JSON"
    fi
elif [ "$2 $3" = "volume rm" ] && [ "${REFUSE_VOLUME:-}" = "$4" ]; then
    exit 1
fi
STUB
cat > "$work/gh" <<'STUB'
#!/bin/bash
case "$2" in
    repos/nzela-org/nzela/*) ;;
    *) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
esac
case "$2" in
    */90)  printf '%s\n' '{"id":90,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"failure","updated_at":"2020-01-01T00:00:00Z"}' ;;
    */100)
        echo "$2" >> "$GH_CALLS"
        if [ "${FLIP_RUN:-}" = 100 ] && [ "$(grep -c '/100$' "$GH_CALLS")" -ge 2 ]; then
            printf '%s\n' '{"id":100,"repository":{"full_name":"nzela-org/nzela"},"status":"in_progress","conclusion":null,"updated_at":"2020-01-01T00:00:00Z"}'
        elif [ "${RUN_RECENT:-}" = 1 ]; then
            printf '{"id":100,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"failure","updated_at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        else
            printf '%s\n' '{"id":100,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"failure","updated_at":"2020-01-01T00:00:00Z"}'
        fi ;;
    */110) printf '%s\n' '{"id":110,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"success","updated_at":"2020-01-01T00:00:00Z"}' ;;
    */120) printf '%s\n' '{"id":120,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"failure","updated_at":"2020-01-01T00:00:00Z"}' ;;
    */130) printf '%s\n' '{"id":130,"repository":{"full_name":"nzela-org/nzela"},"status":"in_progress","conclusion":null,"updated_at":"2020-01-01T00:00:00Z"}' ;;
    */10[0-9][0-9]) printf '{"id":%s,"repository":{"full_name":"nzela-org/nzela"},"status":"completed","conclusion":"failure","updated_at":"2020-01-01T00:00:00Z"}\n' "${2##*/}" ;;
    *) exit 1 ;;
esac
STUB
cat > "$work/busy" <<'STUB'
#!/bin/bash
case " $BUSY " in *" $1 "*) exit 0 ;; esac
exit 1
STUB
chmod +x "$work/docker" "$work/busy" "$work/gh"

cat > "$work/volumes.json" <<'JSON'
{"Volumes":[
{"Name":"nzela-warm-90","Links":"1","Driver":"local","Scope":"local","Size":"5GB"},
{"Name":"nzela-warm-100","Links":"0","Driver":"local","Scope":"local","Size":"20.49GB"},
{"Name":"nzela-warm-110","Links":"0","Driver":"local","Scope":"local","Size":"20.49GB"},
{"Name":"nzela-warm-120","Links":"0","Driver":"local","Scope":"local","Size":"20.49GB"},
{"Name":"nzela-warm-130","Links":"0","Driver":"local","Scope":"local","Size":"20.49GB"},
{"Name":"nzela-warm-shared","Links":"0","Driver":"local","Scope":"local","Size":"40GB"}
]}
JSON

run() {
    local free=$1 busy=$2
    shift 2
    : > "$work/calls"
    : > "$work/gh-calls"
    CALLS="$work/calls" BUSY="$busy" VOLUME_JSON="$work/volumes.json" RUNNER_PRUNE_GH="$work/gh" \
        GH_CALLS="$work/gh-calls" RUNNER_PRUNE_GH_USER="${RUNNER_PRUNE_GH_USER-$(id -un)}" \
        RUNNER_PRUNE_REPOS="${RUNNER_PRUNE_REPOS:-ci=nzela-org/nzela pr=nzela-org/nzela}" RUNNER_PRUNE_FREE_GB=$free RUNNER_PRUNE_POOLS="ci:1 pr:2" \
        RUNNER_PRUNE_DOCKER="$work/docker" RUNNER_PRUNE_BUSY="$work/busy" \
        "$prune_script" "$@" > "$work/output" 2> "$work/errors"
    last_status=$?
}
calls() { grep -c "^$1 " "$work/calls"; }
saw() { grep -q "^$1 .*$2" "$work/calls"; }

run 100 "ci"
saw ci 'volume rm nzela-warm-100' || fail "unrelated busy containers blocked old owned volume cleanup"
! saw ci 'builder prune' || fail "the hourly pass pruned a busy pool's builder"
saw pr "builder prune -af --filter until=2h" || fail "the hourly pass left an idle pool's cache"
saw pr "image prune -af" && fail "the hourly pass removed tagged images, so formal rebuilds its toolchains"

plan_named_pools() {
    env -u RUNNER_PRUNE_REPOS ${1:+RUNNER_PRUNE_REPOS="$1"} CALLS="$work/calls" BUSY="" \
        VOLUME_JSON="$work/volumes.json" RUNNER_PRUNE_GH="$work/gh" GH_CALLS="$work/gh-calls" \
        RUNNER_PRUNE_GH_USER="$(id -un)" RUNNER_PRUNE_FREE_GB=100 RUNNER_PRUNE_POOLS="nzela-ci:1 nzela-pr:2" \
        RUNNER_PRUNE_DOCKER="$work/docker" RUNNER_PRUNE_BUSY="$work/busy" \
        "$prune_script" plan > "$work/output" 2> "$work/errors"
}
plan_named_pools || fail "the default repositories do not find the pull-request pool's runs: $(cat "$work/errors")"
grep -q 'nzela-pr: nzela-warm-100: remove' "$work/output" || fail "the pull-request pool kept a finished run's volume by default"
plan_named_pools 'nzela-ci=nzela-org/nzela nzela-pr=example-fork/nzela'
grep -q 'gh api repos/example-fork/nzela/actions/runs/[0-9]* returned 1: gh: Not Found (HTTP 404)' "$work/errors" \
    || fail "a failed run lookup does not say which call failed and why: $(cat "$work/errors")"

export RUNNER_PRUNE_TMP="$work/tmp"
for pool in ci pr; do
    mkdir -p "$work/tmp/$pool/runner-a"
    touch -d '5 hours ago' "$work/tmp/$pool/runner-a/old"
    touch "$work/tmp/$pool/runner-a/new"
done
run 100 "ci"
[ -e "$work/tmp/pr/runner-a/old" ] && fail "an idle pool kept a temporary file from a finished job"
[ -e "$work/tmp/pr/runner-a/new" ] || fail "an idle pool lost a temporary file from the last hour"
[ -e "$work/tmp/ci/runner-a/old" ] || fail "a busy pool lost a temporary file its job may hold"
unset RUNNER_PRUNE_TMP

run 100 "ci" scheduled
! saw ci 'builder prune' || fail "the scheduled pass pruned a busy pool's builder"
saw pr "builder prune -af --filter until=2h" || fail "the scheduled pass skipped routine cleanup"

run 30 "ci" scheduled
! saw ci 'builder prune' || fail "a low disk pruned a busy pool's builder"
saw pr "image prune -af" || fail "the scheduled pass left an idle pool's low-disk cache"

run 30 "ci" check
! saw ci 'builder prune' || fail "a low disk pruned a busy pool's builder"
saw pr "image prune -af" || fail "a low disk left an idle pool's images"

run 10 "ci pr" check
saw ci "builder prune -af" || fail "a critical disk spared a busy pool"

run 60 "" check
[ -s "$work/calls" ] && fail "a disk with room was pruned on a check"

run 100 "" plan
grep -q 'nzela-warm-100: remove:.*expected bytes 20490000000' "$work/output" || fail "old owned volume absent from plan"
grep -q 'nzela-warm-90: kept: 1 Docker container' "$work/output" || fail "a stopped container reference was ignored"
grep -q 'nzela-warm-130: kept: run 130 is in_progress' "$work/output" || fail "inflight run was ignored"
grep -q 'nzela-warm-110: kept: recent completed run or latest successful cache' "$work/output" || fail "latest green cache was lost"
! grep -q 'nzela-warm-shared:' "$work/output" || fail "stable shared cache entered run retention"
! saw ci 'volume rm' || fail "plan removed a volume"

run 100 "ci" plan
grep -q 'nzela-warm-100: remove:' "$work/output" || fail "a busy pool could not be inspected safely"
! saw ci 'volume rm' || fail "a busy pool's plan removed a volume"

run 100 ""
saw ci 'volume rm nzela-warm-100' || fail "old completed run was not removed"
! saw ci 'volume rm nzela-warm-110' || fail "green cache was removed"
! saw ci 'volume rm nzela-warm-90' || fail "referenced volume was removed"

RUN_RECENT=1 run 100 ""
! saw ci 'volume rm nzela-warm-100' || fail "recent completed run was removed"

REFUSE_VOLUME=nzela-warm-100 run 100 ""
grep -q 'Docker refused nzela-warm-100' "$work/errors" || fail "an in-use race was hidden"
[ "$last_status" -ne 0 ] || fail "Docker refusal returned success"

FLIP_RUN=100 run 100 ""
! saw ci 'volume rm nzela-warm-100' || fail "a newly active run was removed"
grep -q 'changed during cleanup' "$work/errors" || fail "a run-start recheck was hidden"
[ "$last_status" -ne 0 ] || fail "a run-start race returned success"

REF_ON_RECHECK=nzela-warm-100 run 100 ""
! saw ci 'volume rm nzela-warm-100' || fail "a newly referenced volume was removed"
grep -q 'Docker references' "$work/errors" || fail "a reference recheck was hidden"
[ "$last_status" -ne 0 ] || fail "a reference race returned success"

for stream in stdout stderr; do
    marker="$work/$stream-finished"
    FLOOD_STREAM="$stream" FLOOD_MARKER="$marker" run 100 ""
    [ "$last_status" -ne 0 ] || fail "$stream flood returned success"
    [ ! -e "$marker" ] || fail "$stream producer ran beyond the capture cap"
    ! saw ci 'volume rm' || fail "$stream flood authorized volume deletion"
    [ ! -s "$work/errors" ] || grep -q 'exceeds .* bytes' "$work/errors" || fail "$stream overflow was hidden"
    ! grep -q 'private-sentinel' "$work/errors" || fail "$stream output leaked into errors"
done

RUNNER_PRUNE_GH_USER= run 100 "" plan
[ "$last_status" -ne 0 ] || fail "missing GitHub identity returned success"
! saw ci 'volume rm' || fail "missing GitHub identity removed a volume"

RUNNER_PRUNE_GH_USER=root run 100 "" plan
[ "$last_status" -ne 0 ] || fail "root GitHub identity returned success"
grep -q 'must be an unprivileged local account' "$work/errors" || fail "root GitHub identity refusal was hidden"
! saw ci 'volume rm' || fail "root GitHub identity removed a volume"

run 100 "" --help
[ "$last_status" -eq 0 ] && [ ! -s "$work/calls" ] || fail "help touched Docker"
run 100 "" unknown
[ "$last_status" -eq 2 ] && [ ! -s "$work/calls" ] || fail "unknown arguments touched Docker"
run 100 "" plan extra
[ "$last_status" -eq 2 ] && [ ! -s "$work/calls" ] || fail "extra arguments touched Docker"

cp "$work/volumes.json" "$work/base.json"
python3 - "$work/volumes.json" <<'PY'
import json
import sys
volumes = [{"Name": f"nzela-warm-{1000 + index}", "Links": "0", "Driver": "local", "Scope": "local", "Size": "15GB"} for index in range(60)]
volumes[5]["Links"] = "1"
with open(sys.argv[1], "w", encoding="utf-8") as target:
    json.dump({"Volumes": volumes}, target)
PY
run 100 ""
[ "$last_status" -eq 0 ] || fail "an inventory above the old forty-volume limit was refused"
removed=$(grep -c '^ci volume rm ' "$work/calls")
[ "$removed" -eq 57 ] || fail "finished runs' volumes were not all removed from a large inventory: $removed of 57"
saw ci 'volume rm nzela-warm-1000$' || fail "the oldest finished run's volume was kept in a large inventory"
! saw ci 'volume rm nzela-warm-1005$' || fail "a referenced volume was removed from a large inventory"
! saw ci 'volume rm nzela-warm-1059$' || fail "the newest completed run's volume was removed"
! saw ci 'volume rm nzela-warm-1058$' || fail "the second newest completed run's volume was removed"
cp "$work/base.json" "$work/volumes.json"

python3 - "$work/volumes.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    data = json.load(source)
data["Volumes"].extend({"Name": f"foreign-{index}", "Links": "0", "Driver": "local", "Scope": "local", "Size": "1B"} for index in range(500))
with open(sys.argv[1], "w", encoding="utf-8") as target:
    json.dump(data, target)
PY
run 100 "" plan
[ "$last_status" -ne 0 ] || fail "oversized inventory returned success"
! saw ci 'volume rm' || fail "oversized inventory removed a volume"

RUNNER_PRUNE_REPOS='ci=other/repo' run 100 ""
! saw ci 'volume rm' || fail "a different repository's run authorized deletion"

cp "$work/volumes.json" "$work/valid.json"
printf '%s\n' '{"Volumes":[' > "$work/volumes.json"
run 100 ""
! saw ci 'volume rm' || fail "malformed Docker inventory caused deletion"
mv "$work/valid.json" "$work/volumes.json"

cat > "$work/gh" <<'STUB'
#!/bin/bash
printf '%s\n' '{"message":"API failure"}'
STUB
chmod +x "$work/gh"
run 100 ""
! saw ci 'volume rm' || fail "malformed GitHub status caused deletion"

if [ -z "${RUNNER_PRUNE_SCRIPT:-}" ] && ! python3 - "$prune_script" <<'PY'; then
import ast
import os
import select
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

script = Path(sys.argv[1]).read_text(encoding="utf-8").split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
node = next(node for node in ast.parse(script).body if isinstance(node, ast.FunctionDef) and node.name == "call")
scope = {"os": os, "selectors": selectors, "signal": signal, "subprocess": subprocess,
         "time": time, "MAX_STDERR_BYTES": 65536, "deadline": time.monotonic() + 120}
exec(compile(ast.Module(body=[node], type_ignores=[]), "runner-prune-call", "exec"), scope)
call = scope["call"]

assert call([sys.executable, "-c", "print('{\"Volumes\":[]}')"]) == '{"Volumes":[]}\n'
try:
    call([sys.executable, "-c", "import sys; sys.exit(7)"])
except ValueError as error:
    assert "returned 7" in str(error), error
else:
    raise AssertionError("a nonzero child returned success")

with tempfile.TemporaryDirectory() as directory:
    pid_file = os.path.join(directory, "child.pid")
    child = "import signal; signal.pause()"
    producer = ("import os, subprocess, sys, signal; "
                "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]]); "
                "open(sys.argv[1], 'w').write(str(child.pid)); "
                "os.write(1, b'x' * int(sys.argv[3])); signal.pause()")

    def child_exited():
        pid = int(Path(pid_file).read_text(encoding="utf-8"))
        try:
            handle = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            assert select.select([handle], [], [], 2)[0], "a command descendant survived cleanup"
        finally:
            os.close(handle)

    try:
        call([sys.executable, "-c", producer, pid_file, child, "5000"], max_bytes=4096)
    except ValueError as error:
        assert "output exceeds 4096 bytes" in str(error), error
    else:
        raise AssertionError("oversize output returned success")
    child_exited()

    invalid_utf8 = ("import subprocess, sys, os; "
                    "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]], "
                    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                    "open(sys.argv[1], 'w').write(str(child.pid)); "
                    "os.write(1, b'private-sentinel\\xff')")
    try:
        call([sys.executable, "-c", invalid_utf8, pid_file, child])
    except ValueError as error:
        assert "returned invalid UTF-8" in str(error), error
        assert "private-sentinel" not in str(error), error
    else:
        raise AssertionError("invalid UTF-8 returned success")
    child_exited()

    failing = ("import subprocess, sys; "
               "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]], "
               "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
               "open(sys.argv[1], 'w').write(str(child.pid)); sys.exit(7)")
    try:
        call([sys.executable, "-c", failing, pid_file, child])
    except ValueError as error:
        assert "returned 7" in str(error), error
    else:
        raise AssertionError("a failed command returned success")
    child_exited()

    class Clock:
        now = 0
        def monotonic(self):
            return self.now

    clock = Clock()
    original_read = os.read
    def read_then_expire(fd, size):
        chunk = original_read(fd, size)
        if chunk:
            clock.now = 31
        return chunk

    scope["time"] = clock
    scope["os"] = types.SimpleNamespace(read=read_then_expire, killpg=os.killpg)
    scope["deadline"] = 120
    try:
        call([sys.executable, "-c", producer, pid_file, child, "5"], max_bytes=4096)
    except TimeoutError as error:
        assert "deadline" in str(error), error
    else:
        raise AssertionError("an expired command returned success")
    child_exited()

print("bounded capture keeps child exit codes and reaps command groups")
PY
    fail "bounded call did not preserve exits and cleanup"
fi

[ "$failed" -eq 0 ] && echo "run volumes retain active references and recent caches; uncertainty deletes nothing"
exit "$failed"
