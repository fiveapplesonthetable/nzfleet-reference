#!/bin/bash
# deploy/wait-until.sh waits on a check that is a shell function, and stops when the platform
# reports the rollout failed.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/wait-until.XXXXXX")"
trap 'rm -rf "$work"' EXIT
failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }

mkdir -p "$work/deploy"
cp "$here/../../../deploy/wait-until.sh" "$work/deploy/"
cat >"$work/deploy/gcp-run.sh" <<'SCRIPT'
#!/bin/bash
cat "$READY_JSON"
SCRIPT
chmod +x "$work/deploy/gcp-run.sh"

READY_JSON="$work/ready.json"
export READY_JSON
echo '{"status":{"conditions":[{"type":"Ready","status":"Unknown"}]}}' >"$READY_JSON"

# shellcheck source=../../../deploy/wait-until.sh
. "$work/deploy/wait-until.sh"

# The check passes on its third ask, so the wait has to ask again and use the function.
asked=0
third_ask() { asked=$((asked + 1)); [ "$asked" -ge 3 ]; }
wait_until svc region "the third ask" third_ask || fail "a function check was not run in the caller's shell"
[ "$asked" -eq 3 ] || fail "the check was asked $asked times, not 3"

echo '{"status":{"conditions":[{"type":"Ready","status":"False","message":"container failed"}]}}' >"$READY_JSON"
never() { false; }
said="$(wait_until svc region "never" never 2>&1)"
status=$?
[ "$status" -eq 1 ] || fail "a failed rollout did not end the wait (status $status)"
[[ "$said" == *"svc reported its rollout failed while waiting for never: container failed"* ]] ||
    fail "the failed rollout was not named: $said"

# Run as a program, only an executable can be the check.
"$work/deploy/wait-until.sh" svc region "an executable" true || fail "an executable check was not run"

exit "$failed"
