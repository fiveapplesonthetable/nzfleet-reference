#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
good="r/nzela@sha256:$(printf '%064d' 0 | tr 0 a)"
other="r/nzela@sha256:$(printf '%064d' 0 | tr 0 b)"

printf '%s\n' "$good" > "$tmp/digest"
"$root/tools/nzfleet/release/verify-image-digest.sh" "$tmp/digest" "$good"

if "$root/tools/nzfleet/release/verify-image-digest.sh" "$tmp/digest" "$other" 2>"$tmp/err"; then exit 1; fi
grep -q 'ci verified' "$tmp/err"

printf 'latest\n' > "$tmp/digest"
if "$root/tools/nzfleet/release/verify-image-digest.sh" "$tmp/digest" "$good" 2>"$tmp/err"; then exit 1; fi
grep -q 'holds no image digest' "$tmp/err"

: > "$tmp/digest"
if "$root/tools/nzfleet/release/verify-image-digest.sh" "$tmp/digest" "$good" 2>/dev/null; then exit 1; fi
printf '%s\n' 'a digest that differs from what ci recorded is refused, and so is a file without one'
