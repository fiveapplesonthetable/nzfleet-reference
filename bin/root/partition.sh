#!/bin/sh
# The root half of the CPU partition (docs/machine-partition.md).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=${NZFLEET_TMP:-/srv/fleet/tmp}
plan=$(python3 "$here/resource_budget.py" --emit-shell)
eval "$plan"
for uid in 1001 1002; do
    account=$(getent passwd "$uid" | cut -d: -f1)
    runuser -u "$account" -- python3 "$here/runner_shares.py" --uid "$uid" --slice-file "user-$uid.slice.conf" --check
done
for uid in 1000 1001 1002; do
    install -d -m 755 "/etc/systemd/system/user-$uid.slice.d"
    install -m 644 "$here/user-$uid.slice.conf" "/etc/systemd/system/user-$uid.slice.d/50-partition.conf"
done
systemctl daemon-reload
systemctl set-property user-1001.slice "AllowedCPUs=$TRUSTED_CPUS"
systemctl set-property user-1000.slice "AllowedCPUs=$SHARED_CPUS"
systemctl set-property user-1002.slice "AllowedCPUs=$SHARED_CPUS"
for uid in 1001 1002; do
    account=$(getent passwd "$uid" | cut -d: -f1)
    install -d -o "$account" -m 700 "$tmp/$account"
    runuser -u "$account" -- python3 "$here/runner_shares.py" --uid "$uid" --slice-file "user-$uid.slice.conf" --tmp-root "$tmp"
done
