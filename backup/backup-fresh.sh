#!/bin/bash
# Whether the newest database export is recent and not empty.
#
#   PROJECT=... BUCKET=... tools/nzfleet/backup/backup-fresh.sh [max-age-hours]
#
# Exit 0 when the newest export is young enough, 1 when it is measurably not
# (too old, or empty), and 3 when the bucket could not be read, which says
# nothing about the backups. The workflow writes an export a day, so the
# default allows a day and a late run.
#
# Exports are named for the UTC second they started, `nzela/YYYYMMDDTHHMMSS.dump` (db-export.sh) or `.sql.gz` (Cloud SQL's),
# so the newest is read off the name and not off an object timestamp. NZELA_NOW
# (epoch seconds) replaces the clock so the stand-in in backup-fresh-test.sh
# can hold it still.

set -uo pipefail

max_hours="${1:-30}"
now="${NZELA_NOW:-$(date -u +%s)}"

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
said() {
    printf '%s\n' "$*"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
}

[ -n "${PROJECT:-}" ] || { fail "backup-fresh: PROJECT is unset."; exit 3; }
[ -n "${BUCKET:-}" ] || { fail "backup-fresh: BUCKET is unset."; exit 3; }
[[ "$max_hours" =~ ^[1-9][0-9]*$ ]] || { fail "backup-fresh: the age is whole hours."; exit 3; }

# `SIZE  TIMESTAMP  gs://bucket/nzela/NAME` per object, and a total line last.
listing="$(gcloud storage ls -l --project "$PROJECT" "gs://$BUCKET/nzela/" </dev/null 2>&1)" || {
    said "the export bucket could not be read, so nothing here says whether the backups are current"
    printf '%s\n' "$listing" >&2
    exit 3
}

newest="$(printf '%s\n' "$listing" |
    awk '$3 ~ /\/nzela\/[0-9]{8}T[0-9]{6}\.(sql\.gz|dump)$/ { n = $3; sub(/^.*\//, "", n); print n, $1 }' |
    sort | tail -n 1)"
if [ -z "$newest" ]; then
    said "the bucket holds no export at all"
    exit 1
fi

name="${newest% *}"
size="${newest#* }"
stamp="${name%%.*}"
started="$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null)" || {
    said "the newest export, $name, has a name this cannot read"
    exit 3
}
hours=$(( (now - started) / 3600 ))

if [ "$size" -eq 0 ]; then
    said "the newest export, $name, is empty"
    exit 1
fi
if [ "$hours" -gt "$max_hours" ]; then
    said "the newest export is $name, $hours hours old, and more than $max_hours is stale"
    exit 1
fi
said "the newest export is $name, $hours hours old, $size bytes"
