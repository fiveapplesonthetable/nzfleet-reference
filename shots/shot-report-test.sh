#!/bin/bash
# What shot-report.sh promises, against a scratch repository of two screenshots, one of them golden.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }

mkdir -p "$work/tools/nzfleet/shots" "$work/shots/screenshots"
cp "$here/shot-report.sh" "$work/tools/nzfleet/shots/"
mkdir -p "$work/tools/nzfleet/bin" "$work/tools/nzfleet/nzfleet"
cp "$here/../bin/nzfleet-select" "$work/tools/nzfleet/bin/"
cp -r "$here/../nzfleet/selection" "$work/tools/nzfleet/nzfleet/"
git -C "$work" init -q
printf '# comment\nheld.png\n' > "$work/shots/screenshots.golden"
printf 'one' > "$work/shots/screenshots/held.png"
printf 'one' > "$work/shots/screenshots/loose.png"
git -C "$work" add -A
git -C "$work" -c user.name=t -c user.email=t@t commit -qm base

report() { (cd "$work" && SHOT_REPORT="$work/out" tools/nzfleet/shots/shot-report.sh console shots/screenshots.golden shots/screenshots 2>&1); }

report >/dev/null || fail "a run that redrew nothing was refused"

printf 'two' > "$work/shots/screenshots/loose.png"
printf 'new' > "$work/shots/screenshots/added.png"
said="$(report)" || fail "a changed shot outside the golden set failed the area"
[[ "$said" == *"changed"*"loose.png"* && "$said" == *"new"*"added.png"* ]] || fail "the changed shots were not listed"
[ "$(cat "$work/out/console/before/shots/screenshots/loose.png")" = one ] || fail "the report lost the old image"
[ "$(cat "$work/out/console/after/shots/screenshots/loose.png")" = two ] || fail "the report lost the new image"
grep -q 'before/shots/screenshots/loose.png' "$work/out/console/index.html" || fail "the page does not show the pair"

printf 'two' > "$work/shots/screenshots/held.png"
report >/dev/null && fail "a changed golden shot passed"
said="$(NZELA_SCREENSHOT_REVIEW_ONLY=1 report)" || fail "review-only rejected a changed golden shot"
[[ "$said" == *"changed"*"held.png"*"golden"* ]] || fail "review-only hid the changed golden shot"

rm "$work/shots/screenshots/held.png"
report >/dev/null && fail "a golden shot that is gone passed"
NZELA_SCREENSHOT_REVIEW_ONLY=1 report >/dev/null && fail "review-only accepted a removed golden shot"

git -C "$work" checkout -q -- .
rm -f "$work/shots/screenshots/added.png"
printf 'held.png\nmissing.png\n' > "$work/shots/screenshots.golden"
report >/dev/null && fail "a golden entry naming no screenshot passed"
NZELA_SCREENSHOT_REVIEW_ONLY=1 report >/dev/null && fail "review-only accepted a golden entry naming no screenshot"

[ "$failed" -eq 0 ] && echo "shot-report: golden drift is held except in review-only runs; missing shots still fail"
exit "$failed"
