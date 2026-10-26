#!/bin/bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
env_file="$work/env"
names='BLUEFIN_ENVIRONMENT|BLUEFIN_API_KEY|FCM_SERVICE_ACCOUNT'

cat > "$env_file" <<'ENV'
DATABASE_URL=postgres://localhost/nzela
BLUEFIN_ENVIRONMENT=old
BLUEFIN_API_KEY=old
FCM_SERVICE_ACCOUNT=old
ENV

write_current_values() {
    printf 'BLUEFIN_ENVIRONMENT=sandbox\nBLUEFIN_API_KEY=current\n' |
        sh "$root/deploy/write-host-env.sh" "$env_file" "$names"
}

write_current_values
write_current_values

for name in BLUEFIN_ENVIRONMENT BLUEFIN_API_KEY; do
    count="$(grep -c "^${name}=" "$env_file")"
    [ "$count" -eq 1 ] || { echo "$name appears $count times" >&2; exit 1; }
done
! grep -q '^FCM_SERVICE_ACCOUNT=' "$env_file"
grep -qx 'DATABASE_URL=postgres://localhost/nzela' "$env_file"
grep -qx 'BLUEFIN_ENVIRONMENT=sandbox' "$env_file"
grep -qx 'BLUEFIN_API_KEY=current' "$env_file"
echo "host .env credentials are replaced once per name"
