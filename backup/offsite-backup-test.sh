#!/bin/bash
# What offsite-backup.sh promises, against stand-ins for gcloud, ssh and rsync.
# The stand-in ssh runs the remote command here, and the "far machine" is a
# directory.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=""
mkdir -p "$work/bin" "$work/bucket" "$work/far" "$work/tmp"
printf 'key' > "$work/reader.json"

fail() { failed="$failed $1"; shift; echo "  $*" >&2; }

cat > "$work/bin/gcloud" <<'STUB'
#!/bin/bash
echo "${CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE:-none} $*" >> "$WORK/gcloud.log"
[ -d "$BUCKET_DIR" ] || { echo "ERROR: 403" >&2; exit 1; }
case "$2" in
    ls)
        for f in "$BUCKET_DIR"/*.sql.gz; do
            [ -e "$f" ] || continue
            printf '%10s  2026-10-15T06:17:00Z  gs://b/nzela/%s\n' "$(stat -c %s "$f")" "$(basename "$f")"
        done
        echo "TOTAL: objects" ;;
    cp)
        src="${@: -2:1}"; dst="${@: -1}"
        cp "$BUCKET_DIR/$(basename "$src")" "$dst"
        [ -z "${SHORT_DOWNLOAD:-}" ] || head -c 10 "$BUCKET_DIR/$(basename "$src")" > "$dst" ;;
esac
STUB
cat > "$work/bin/ssh" <<'STUB'
#!/bin/bash
while [ "$#" -gt 2 ] && [ "${1:0:1}" = - ]; do
    case "$1" in -o|-i) shift 2 ;; *) shift ;; esac
done
shift
bash -c "$*"
STUB
cat > "$work/bin/rsync" <<'STUB'
#!/bin/bash
dst="${@: -1}"; src="${@: -2:1}"
cp "$src" "${dst#*:}"
[ -z "${CORRUPT_ARRIVAL:-}" ] || printf 'x' >> "${dst#*:}"
STUB
chmod +x "$work/bin/"*

export_obj() { printf 'dump %s\n-- PostgreSQL database dump complete\n' "$1" | gzip > "$work/bucket/$1.sql.gz"; }

run() {
    ( cd "$here/../../.." && env PATH="$work/bin:$PATH" WORK="$work" BUCKET_DIR="$work/bucket" TMPDIR="$work/tmp" \
        PROJECT=p BUCKET=b NZELA_OFFSITE_DIR="$work/far" NZELA_OFFSITE_HOST=nzbackup@devvm \
        NZELA_OFFSITE_READER_KEY="$work/reader.json" "$@" tools/nzfleet/backup/offsite-backup.sh >"$work/out" 2>&1 )
}
status() { run "$@"; echo $?; }
reset() { rm -rf "$work/bucket" "$work/far" "$work/gcloud.log"; mkdir -p "$work/bucket" "$work/far"; }

the_newest_export_is_copied_and_named() {
    reset; export_obj 20261001T061700; export_obj 20261002T061700
    [ "$(status)" = 0 ] || fail "$1" "the copy failed"
    [ -f "$work/far/20261002T061700.sql.gz" ] || fail "$1" "the newest export did not arrive"
    [ ! -f "$work/far/20261001T061700.sql.gz" ] || fail "$1" "an older export was copied too"
    grep -q "copied 20261002T061700" "$work/out" || fail "$1" "the output did not say what was copied"
}

a_second_run_copies_nothing() {
    reset; export_obj 20261002T061700
    run; : > "$work/gcloud.log"
    [ "$(status)" = 0 ] || fail "$1" "the second run failed"
    grep -q "already on" "$work/out" || fail "$1" "the second run did not say it had nothing to do"
    grep -q " cp " "$work/gcloud.log" && fail "$1" "the second run downloaded again"
    return 0
}

only_the_newest_thirty_are_kept() {
    reset
touch "$work"/far/202609{01..30}T061700.sql.gz
    export_obj 20261003T061700
    run
    [ "$(ls "$work/far" | wc -l)" = 30 ] || fail "$1" "the far side holds $(ls "$work/far" | wc -l) copies instead of 30"
    [ -f "$work/far/20261003T061700.sql.gz" ] || fail "$1" "the newest copy was pruned"
    [ ! -f "$work/far/20260901T061700.sql.gz" ] || fail "$1" "the oldest copy was kept"
}

a_run_without_the_reader_key_does_nothing() {
    reset; export_obj 20261002T061700
    [ "$(status NZELA_OFFSITE_READER_KEY=)" = 3 ] || fail "$1" "a run with no key did not exit 3"
    [ ! -f "$work/gcloud.log" ] || fail "$1" "gcloud was called with no key"
    [ -z "$(ls "$work/far")" ] || fail "$1" "something was copied with no key"
}

gcloud_reads_as_the_reader_and_not_the_machines_login() {
    reset; export_obj 20261002T061700
    run
    grep -v "^$work/reader.json " "$work/gcloud.log" && fail "$1" "a gcloud call ran under another credential"
    return 0
}

a_download_that_is_not_the_listed_size_is_refused() {
    reset; export_obj 20261002T061700
    [ "$(status SHORT_DOWNLOAD=1)" = 1 ] || fail "$1" "a short download was not refused"
    [ -z "$(ls "$work/far")" ] || fail "$1" "a short download was sent"
}

a_copy_that_arrives_different_is_removed() {
    reset; export_obj 20261002T061700
    [ "$(status CORRUPT_ARRIVAL=1)" = 1 ] || fail "$1" "a corrupted arrival was not refused"
    [ -z "$(ls "$work/far")" ] || fail "$1" "the corrupted copy was left on the far side"
}

an_unreadable_bucket_is_not_a_finding_about_the_backups() {
    reset; rm -rf "$work/bucket"
    [ "$(status)" = 3 ] || fail "$1" "an unreadable bucket did not exit 3"
}

no_temporary_file_outlives_a_run() {
    reset; export_obj 20261002T061700
    run; status SHORT_DOWNLOAD=1 >/dev/null
    [ -z "$(ls "$work/tmp")" ] || fail "$1" "a temporary directory was left behind"
}

for property in \
    the_newest_export_is_copied_and_named \
    a_second_run_copies_nothing \
    only_the_newest_thirty_are_kept \
    a_run_without_the_reader_key_does_nothing \
    gcloud_reads_as_the_reader_and_not_the_machines_login \
    a_download_that_is_not_the_listed_size_is_refused \
    a_copy_that_arrives_different_is_removed \
    an_unreadable_bucket_is_not_a_finding_about_the_backups \
    no_temporary_file_outlives_a_run
do
    echo "$property"
    "$property" "$property"
done

[ -z "$failed" ] || { echo "failed:$failed" >&2; exit 1; }
