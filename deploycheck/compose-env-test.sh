#!/bin/bash
# deploy/compose.yaml forwards every provider credential and mode that
# config/providers.rs reads, so a worker started by `make up-full` sees what `serve` does.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
root="$here/../../.."
bad=0

names="$(grep -ohE '"(BLUEFIN|HONEYCOIN)_(API_KEY|API_SECRET|PUBLIC_KEY|WEBHOOK_SECRET|ENVIRONMENT|BASE_URL)"' \
    "$root/crates/nzela-config/src/providers.rs" | tr -d '"' | sort -u)"
[ -n "$names" ] || { echo "  config/providers.rs names no provider variable" >&2; exit 1; }

for name in $names; do
    grep -qE "^ +$name:" "$root/deploy/compose.yaml" || { echo "  compose.yaml does not forward $name" >&2; bad=1; }
done

[ "$bad" -eq 0 ] && echo "compose forwards every provider variable the service reads"

python3 -I - "$root/deploy/compose.prod.yaml" <<'PY' || { echo "  production Compose does not run one unbounded worker and an opt-in sanctions refresh" >&2; bad=1; }
import sys, yaml
services = yaml.safe_load(open(sys.argv[1]))["services"]
worker = services.get("worker", {})
refresh = services.get("sanctions-refresh", {})
ok = (
    worker.get("command") == ["work"]
    and worker.get("restart") == "unless-stopped"
    and "sweep" not in services
    and refresh.get("profiles") == ["scheduled"]
    and refresh.get("command") == ["sanctions", "refresh"]
)
sys.exit(0 if ok else 1)
PY
for gone in nzela-sweep.service nzela-sweep.timer; do
    [ ! -e "$root/deploy/$gone" ] || { echo "  deploy/$gone still starts work from a timer" >&2; bad=1; }
done
if grep -qE '^StartLimit' "$root/deploy/nzela-work.service"; then
    echo "  nzela-work.service can stop for good after repeated failures" >&2
    bad=1
fi
grep -q 'run --rm --no-deps sanctions-refresh$' "$root/deploy/nzela-sanctions-refresh.service" \
    || { echo "  the sanctions timer does not invoke its Compose service without dependencies" >&2; bad=1; }
grep -q 'OnCalendar=\*-\*-\* 02:00:00 UTC' "$root/deploy/nzela-sanctions-refresh.timer" \
    || { echo "  the Compose sanctions refresh is not scheduled daily" >&2; bad=1; }

[ "$bad" -eq 0 ] && echo "production Compose keeps one worker running and schedules sanctions refresh daily"
exit "$bad"
