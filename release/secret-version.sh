#!/bin/bash
#
# Adds a version of an operator-supplied credential and prints its number.
#
#   tools/nzfleet/release/secret-version.sh nzela-bluefin-api-key
#
# The value is read from standard input, never from an argument, so it is not
# in shell history or a process listing, and Terraform never sees it: the number
# printed is what goes in `secret_versions`. Run it as an identity allowed to
# add versions to that one secret.

set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
id="${1:-}"

known="$(awk -F= 'NF == 2 && $2 != "" { print $2 }' "$root/deploy/secrets.list")"
grep -qx -- "$id" <<<"$known" || {
    echo "usage: tools/nzfleet/release/secret-version.sh <secret-id>, one of:" >&2
    printf '  %s\n' $known >&2
    exit 2
}

if [ -t 0 ]; then
    read -r -s -p "$id: " value
    printf '\n' >&2
else
    value="$(cat)"
fi
[ -n "$value" ] || { echo "secret-version: the value is empty, so no version was added." >&2; exit 1; }

printf '%s' "$value" |
    gcloud secrets versions add "$id" --data-file=- --format='value(name.basename())'
