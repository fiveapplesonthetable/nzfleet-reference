#!/bin/bash
# Export DATABASE_URL to ARTIFACT_STORE and print the resulting URL.
# ARTIFACT_STORE accepts file://, gs:// and s3:// destinations.

set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is unset}"
: "${ARTIFACT_STORE:?ARTIFACT_STORE is unset}"

case "$ARTIFACT_STORE" in
    file://*) uploader=cp; destination=${ARTIFACT_STORE#file://} ;;
    gs://*) uploader=gsutil; destination=$ARTIFACT_STORE ;;
    s3://*) uploader=aws; destination=$ARTIFACT_STORE ;;
    *) echo "backup: ARTIFACT_STORE must use file://, gs:// or s3://" >&2; exit 2 ;;
esac

command -v pg_dump >/dev/null || { echo "backup: pg_dump is unavailable" >&2; exit 3; }
command -v "$uploader" >/dev/null || { echo "backup: $uploader is unavailable" >&2; exit 3; }

stamp="$(date -u +%Y%m%dT%H%M%S)"
work="$(mktemp -d "${TMPDIR:-/tmp}/nzela-backup.XXXXXX")"
trap 'rm -rf "$work"' EXIT
archive="$work/$stamp.sql.gz"
pg_dump --format=plain --no-owner --no-privileges "$DATABASE_URL" | gzip -c > "$archive"
test -s "$archive"

case "$uploader" in
    cp)
        mkdir -p "$destination/nzela"
        cp "$archive" "$destination/nzela/"
        result="$destination/nzela/$stamp.sql.gz"
        ;;
    gsutil)
        gsutil cp "$archive" "$destination/nzela/$stamp.sql.gz"
        result="$destination/nzela/$stamp.sql.gz"
        ;;
    aws)
        aws s3 cp "$archive" "$destination/nzela/$stamp.sql.gz"
        result="$destination/nzela/$stamp.sql.gz"
        ;;
esac
printf '%s\n' "$result"
