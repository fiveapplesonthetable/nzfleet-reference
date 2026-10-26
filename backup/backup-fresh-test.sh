#!/bin/bash
# What backup-fresh.sh promises, against a stand-in gcloud and a held clock.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=""
mkdir -p "$work/bin"

fail() { failed="$failed $1"; shift; echo "  $*" >&2; }

cat > "$work/bin/gcloud" <<'STUB'
#!/bin/bash
case " $* " in
    *" --project some-project "*) ;;
    *) echo "ERROR: no project" >&2; exit 1 ;;
esac
[ -f "$LISTING" ] || { echo "ERROR: 403" >&2; exit 1; }
cat "$LISTING"
STUB
chmod +x "$work/bin/gcloud"

# 2026-09-30T12:00:00Z
now="$(date -u -d 2026-09-30T12:00:00 +%s)"
listing() { printf '%s\n' "$@" > "$work/listing"; }
obj() { printf '%10s  2026-09-30T06:17:00Z  gs://b/nzela/%s.sql.gz' "$2" "$1"; }

run() {
    ( cd "$here/../../.." && PATH="$work/bin:$PATH" LISTING="$work/listing" NZELA_NOW="$now" \
        PROJECT=some-project BUCKET=b GITHUB_STEP_SUMMARY="$work/summary" "$@" tools/nzfleet/backup/backup-fresh.sh >"$work/out" 2>&1 )
}
status() { run env; echo $?; }

a_recent_export_is_fresh() {
    listing "$(obj 20260930T061700 4096)"
    [ "$(status)" = 0 ] || fail "$1" "a six hour old export was called stale"
}

an_export_older_than_the_window_is_stale() {
    listing "$(obj 20260928T061700 4096)"
    [ "$(status)" = 1 ] || fail "$1" "a two day old export was not called stale"
}

the_newest_export_is_the_one_judged() {
    listing "$(obj 20260901T061700 4096)" "$(obj 20260930T061700 4096)" "$(obj 20260915T061700 4096)"
    [ "$(status)" = 0 ] || fail "$1" "an old export hid the newest"
}

an_empty_export_is_not_a_backup() {
    listing "$(obj 20260930T061700 0)"
    [ "$(status)" = 1 ] || fail "$1" "a zero byte export was called fresh"
}

an_empty_bucket_is_stale_and_not_unknown() {
    listing "TOTAL: 0 objects"
    [ "$(status)" = 1 ] || fail "$1" "a bucket with nothing in it was not called stale"
}

a_bucket_that_cannot_be_read_is_unknown_and_not_stale() {
    rm -f "$work/listing"
    [ "$(status)" = 3 ] || fail "$1" "an unreadable bucket did not exit 3"
    grep -q "hours old" "$work/out" && fail "$1" "an unreadable bucket was called stale"
    return 0
}

a_check_with_no_project_asks_nothing() {
    listing "$(obj 20260930T061700 4096)"
    ( cd "$here/../../.." && env -u PROJECT PATH="$work/bin:$PATH" LISTING="$work/listing" BUCKET=b tools/nzfleet/backup/backup-fresh.sh >/dev/null 2>&1 )
    [ $? = 3 ] || fail "$1" "a check with no project did not exit 3"
}

for property in \
    a_recent_export_is_fresh \
    an_export_older_than_the_window_is_stale \
    the_newest_export_is_the_one_judged \
    an_empty_export_is_not_a_backup \
    an_empty_bucket_is_stale_and_not_unknown \
    a_bucket_that_cannot_be_read_is_unknown_and_not_stale \
    a_check_with_no_project_asks_nothing
do
    echo "$property"
    "$property" "$property"
done

[ -z "$failed" ] || { echo "failed:$failed" >&2; exit 1; }
