#!/bin/bash
#
# The device lab's stack, started on a port of its own and held to what it
# promises: a verified sender to sign in as, a world that reset restores, and an
# `up` that does nothing the second time.
#
#   DATABASE_URL=postgres://... tools/nzfleet/devloop/device-lab-test.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
[ -n "${DATABASE_URL:-}" ] || { echo "device-lab-test: DATABASE_URL is unset" >&2; exit 1; }

PORT="$(python3 - <<'PY'
import socket
while True:
    first = socket.socket()
    first.bind(("127.0.0.1", 0))
    port = first.getsockname()[1]
    try:
        held = [socket.socket() for _ in range(3)]
        for offset, s in enumerate(held, 1):
            s.bind(("127.0.0.1", port + offset))
    except OSError:
        continue
    print(port)
    break
PY
)"
export DEVICE_LAB_PORT="$PORT" DEVICE_LAB_SWEEP_TICK=manual DEVICE_LAB_BIND=127.0.0.1
export DEVICE_LAB_DIR="${TMPDIR:-/tmp}/device-lab-test-$PORT"
LAB=tools/nzfleet/devloop/device-lab.sh
BASE="http://127.0.0.1:$PORT"

fail() { echo "device-lab-test: $*" >&2; exit 1; }
trap '$LAB down >/dev/null 2>&1' EXIT

sign_in() {
    curl -sf -X POST "$BASE/v1/sessions/development" -H 'content-type: application/json' \
        -d '{"email":"amina@example.com","full_name":"Amina Kabila"}' |
        python3 -c 'import json,sys;print(json.load(sys.stdin)["session_token"])'
}
me_status() { curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/me" -H "Authorization: Bearer $1"; }

$LAB up >/dev/null || fail "the stack did not come up"

curl -sf "$BASE/health" >/dev/null || fail "the API does not answer through the forwarder"

token="$(sign_in)" || fail "the development sign-in does not work through the forwarder"
curl -sf "$BASE/v1/me" -H "Authorization: Bearer $token" |
    python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin)["verification"]["status"] == "VERIFIED" else 1)' ||
    fail "the seeded sender is not verified"

before="$(cat "$DEVICE_LAB_DIR"/api.pid)"
$LAB up >/dev/null || fail "a second up failed"
[ "$before" = "$(cat "$DEVICE_LAB_DIR"/api.pid)" ] || fail "a second up restarted the API"

$LAB reset >/dev/null || fail "reset failed"
[ "$(me_status "$token")" = 401 ] || fail "a session from before the reset still works after it"
sign_in >/dev/null || fail "the development sign-in does not work after a reset"

$LAB sweep | grep -q collections_resolved || fail "a manual sweep did not run a pass"
echo "device-lab-test: ok"
