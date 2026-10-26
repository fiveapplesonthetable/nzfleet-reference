#!/bin/bash
# Writes a pg_dump of the database to a destination and checks it arrived.
#
#   DATABASE_URL=postgres://... tools/nzfleet/backup/db-export.sh gs://bucket/nzela/
#   tools/nzfleet/backup/db-export.sh file:///var/backups/nzela/
#   tools/nzfleet/backup/db-export.sh ssh://nzbackup@host:22/srv/nzbackup/
#
# The dump is custom format, compressed, without owners or grants, so it loads
# into any server whatever roles it has. It is named for the UTC second it
# started, `YYYYMMDDTHHMMSS.dump`, which is what backup-fresh.sh and the other
# readers sort on. The size and sha256 are read back from the destination and
# compared with the file that left; gs:// reads them from the upload's own
# response, because the account that writes the bucket may not read it.
#
# Exit 0 when the dump is on the far side, 1 when what arrived does not match
# what left, 3 when it could not run. NZELA_EXPORT_SSH_KEY names an ssh key, and
# NZELA_GCS_ENDPOINT replaces https://storage.googleapis.com for the test.

set -euo pipefail

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
unable() { fail "db-export: $*"; exit 3; }
broken() { fail "db-export: $*"; exit 1; }

dest="${1:-${NZELA_EXPORT_DEST:-}}"
[ -n "$dest" ] || unable "name a destination: gs://, file:// or ssh:// URL."
[ -n "${DATABASE_URL:-}" ] || unable "DATABASE_URL is unset."
dest="${dest%/}"
case "$dest" in
gs://* | file://* | ssh://*) ;;
s3://*) unable "s3:// is not implemented." ;;
*) unable "$dest is not a gs://, file:// or ssh:// URL." ;;
esac

work="$(mktemp -d -t nzela-db-export.XXXXXX)"
trap 'rm -rf "$work"' EXIT

server_version="$(psql -X -q -At "$DATABASE_URL" -c "select current_setting('server_version_num')" 2>"$work/psql.err")" \
    || { cat "$work/psql.err" >&2; unable "could not read the database server version."; }
server_major="$((server_version / 10000))"

client_major="$(pg_dump --version 2>/dev/null | sed -nE 's/^.*PostgreSQL\) ([0-9]+).*/\1/p')" \
    || unable "PostgreSQL $server_major server requires matching pg_dump, but none is available in this image."
[ "$client_major" = "$server_major" ] \
    || unable "PostgreSQL $server_major server requires matching pg_dump, but this image has ${client_major:-no PostgreSQL client}."

name="$(date -u +%Y%m%dT%H%M%S).dump"
file="$work/$name"

pg_dump --format=custom --compress=6 --no-owner --no-privileges --dbname "$DATABASE_URL" --file "$file" \
    2>"$work/pg_dump.err" || { cat "$work/pg_dump.err" >&2; unable "pg_dump failed."; }
# A file pg_restore cannot list is not a backup, whatever its size.
pg_restore --list "$file" >/dev/null 2>"$work/pg_restore.err" \
    || { cat "$work/pg_restore.err" >&2; broken "the dump pg_restore cannot read."; }

size="$(stat -c %s "$file")"
sum="$(sha256sum "$file" | cut -d' ' -f1)"
[ "$size" -gt 0 ] || broken "the dump is empty."

expect() {
    [ "$1" = "$size" ] || broken "$name is $1 bytes at $dest and $size here."
    [ "$2" = "$sum" ] || broken "$name has sha256 $2 at $dest and $sum here."
}

# Written under a temporary name and renamed, so a copy that stops half way is
# never listed under the dump's name.
to_file() {
    local dir="${dest#file://}"
    mkdir -p "$dir" && cp "$file" "$dir/.$name.part" && mv "$dir/.$name.part" "$dir/$name" \
        || unable "could not write $dir."
    expect "$(stat -c %s "$dir/$name")" "$(sha256sum "$dir/$name" | cut -d' ' -f1)"
}

to_ssh() {
    local rest="${dest#ssh://}" host path port=()
    host="${rest%%/*}"
    path="/${rest#*/}"
    if [[ "$host" == *:* ]]; then
        port=(-p "${host##*:}")
        host="${host%:*}"
    fi
    local opts=(-o BatchMode=yes -o ConnectTimeout=20 "${port[@]}")
    [ -z "${NZELA_EXPORT_SSH_KEY:-}" ] || opts+=(-i "$NZELA_EXPORT_SSH_KEY")
    ssh "${opts[@]}" "$host" "mkdir -p '$path' && cat > '$path/.$name.part' && mv '$path/.$name.part' '$path/$name'" \
        <"$file" || unable "could not send $name to $host."
    local there
    there="$(ssh "${opts[@]}" "$host" "stat -c %s '$path/$name' && sha256sum '$path/$name'")" \
        || unable "could not read $name back from $host."
    expect "$(sed -n 1p <<<"$there")" "$(sed -n 2p <<<"$there" | cut -d' ' -f1)"
}

# One media upload as the caller's own gcloud identity. The upload's response
# carries the stored size and md5, and the md5 sent with the request makes
# Cloud Storage refuse bytes that do not match it; sha256 is not something the
# object records, so the md5 stands for the checksum here.
to_gcs() {
    local rest="${dest#gs://}" bucket object token md5 reply
    bucket="${rest%%/*}"
    object="${rest#*/}/$name"
    [ "$bucket" != "$rest" ] || object="$name"
    token="$(gcloud auth print-access-token 2>"$work/token.err")" \
        || { cat "$work/token.err" >&2; unable "no gcloud credential to upload with."; }
    md5="$(openssl dgst -md5 -binary "$file" | base64)"
    reply="$(curl -sS --fail-with-body -X POST \
        -H "Authorization: Bearer $token" \
        -H "x-goog-hash: md5=$md5" \
        -H "Content-Type: application/octet-stream" \
        --data-binary "@$file" \
        "${NZELA_GCS_ENDPOINT:-https://storage.googleapis.com}/upload/storage/v1/b/$bucket/o?uploadType=media&name=$(printf %s "$object" | sed 's|/|%2F|g')" \
        2>"$work/curl.err")" || { cat "$work/curl.err" >&2; printf '%s\n' "$reply" >&2; unable "the upload to $dest failed."; }
    local stored_size stored_md5
    stored_size="$(sed -nE 's/.*"size": *"([0-9]+)".*/\1/p' <<<"$reply")"
    stored_md5="$(sed -nE 's/.*"md5Hash": *"([^"]+)".*/\1/p' <<<"$reply")"
    [ -n "$stored_size" ] && [ -n "$stored_md5" ] || { printf '%s\n' "$reply" >&2; unable "the upload's reply names no size or checksum."; }
    [ "$stored_size" = "$size" ] || broken "$name is $stored_size bytes at $dest and $size here."
    [ "$stored_md5" = "$md5" ] || broken "$name has md5 $stored_md5 at $dest and $md5 here."
    sum="md5 $md5"
}

case "$dest" in
file://*) to_file ;;
ssh://*) to_ssh ;;
gs://*) to_gcs ;;
esac

echo "db-export: wrote $name ($size bytes, ${sum:0:19}) to $dest"
