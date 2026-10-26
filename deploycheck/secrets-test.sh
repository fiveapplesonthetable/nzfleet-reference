#!/bin/bash
set -uo pipefail
root="$(cd "$(dirname "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/scripts/areas" "$work/scripts/lib"
cp "$root/scripts/areas/secrets.sh" "$work/scripts/areas/"
mkdir -p "$work/tools/nzfleet/bin" "$work/tools/nzfleet/nzfleet"
cp "$root/tools/nzfleet/bin/nzfleet-select" "$work/tools/nzfleet/bin/"
cp -r "$root/tools/nzfleet/nzfleet/selection" "$work/tools/nzfleet/nzfleet/"
git -C "$work" init -q
git -C "$work" config user.name fixture
git -C "$work" config user.email fixture@example.test
printf '%s\n' "AKIA""0123456789ABCDEF" > "$work/credential with spaces.txt"
git -C "$work" add .
source "$work/scripts/areas/secrets.sh"
note() { :; }
fail() { :; }
if (cd "$work" && scan_secrets >/dev/null 2>&1); then
    echo "secrets: a credential in a spaced path passed" >&2
    exit 1
fi
echo "secrets: a credential in a spaced path is detected"
rm "$work/credential with spaces.txt"
git -C "$work" add -A
if (cd "$work" && scan_secrets >/dev/null 2>&1); then
    echo "secrets: a clean tracked inventory passes"
else
    echo "secrets: a clean tracked inventory was refused" >&2
    exit 1
fi
