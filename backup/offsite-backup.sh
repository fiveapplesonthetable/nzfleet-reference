#!/bin/bash
# Copies the newest database export to a machine outside the project.
#
#   NZELA_OFFSITE_READER_KEY=reader.json PROJECT=... BUCKET=... tools/nzfleet/backup/offsite-backup.sh
#
# Runs on the machine that holds the Google credential; the destination never
# does. The key is the nzela-offsite-reader service account's, which may read the
# export bucket and nothing else, and it is handed to gcloud for this process
# only, so the machine's own gcloud login is untouched. Exits 0 when the newest
# export is on the far side, whether this run put it there or an earlier one
# did, 1 when what arrived does not match what left, 3 when it could not run.
#
# NZELA_OFFSITE_HOST (default nzbackup@devvm), NZELA_OFFSITE_DIR (default
# /srv/nzbackup), NZELA_OFFSITE_SSH_KEY and NZELA_OFFSITE_KEEP (default 30).

set -uo pipefail

host="${NZELA_OFFSITE_HOST:-nzbackup@devvm}"
dir="${NZELA_OFFSITE_DIR:-/srv/nzbackup}"
keep="${NZELA_OFFSITE_KEEP:-30}"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=20)
[ -z "${NZELA_OFFSITE_SSH_KEY:-}" ] || ssh_opts+=(-i "$NZELA_OFFSITE_SSH_KEY")

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
unable() { fail "offsite-backup: $*"; exit 3; }
broken() { fail "offsite-backup: $*"; exit 1; }

[ -n "${NZELA_OFFSITE_READER_KEY:-}" ] || unable "NZELA_OFFSITE_READER_KEY is unset; it names the reader service account's key file."
[ -s "$NZELA_OFFSITE_READER_KEY" ] || unable "$NZELA_OFFSITE_READER_KEY is missing or empty."
[ -n "${PROJECT:-}" ] && [ -n "${BUCKET:-}" ] || unable "PROJECT and BUCKET are unset."
[[ "$keep" =~ ^[1-9][0-9]*$ ]] || unable "NZELA_OFFSITE_KEEP is a whole number of copies."

work="$(mktemp -d -t nzela-offsite.XXXXXX)"
trap 'rm -rf "$work"' EXIT

# Both gcloud calls read as the reader; nothing else on this machine does.
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$NZELA_OFFSITE_READER_KEY"
remote() { ssh "${ssh_opts[@]}" "$host" "$@"; }

listing="$(gcloud storage ls -l --project "$PROJECT" "gs://$BUCKET/nzela/" </dev/null 2>&1)" || {
    printf '%s\n' "$listing" >&2
    unable "the export bucket could not be read."
}
newest="$(printf '%s\n' "$listing" |
    awk '$3 ~ /\/nzela\/[0-9]{8}T[0-9]{6}\.(sql\.gz|dump)$/ { n = $3; sub(/^.*\//, "", n); print n, $1, $3 }' |
    sort | tail -n 1)"
[ -n "$newest" ] || broken "the bucket holds no export."
read -r name size uri <<<"$newest"
[ "$size" -gt 0 ] || broken "the newest export, $name, is empty."

have="$(remote "ls -1 '$dir'" 2>&1)" || { printf '%s\n' "$have" >&2; unable "$host:$dir could not be read."; }
if printf '%s\n' "$have" | grep -qx "$name"; then
    echo "offsite-backup: $name is already on $host, nothing to copy"
    exit 0
fi

# gcloud checks the object's own checksum as it downloads; the size and gzip
# test below catch a listing that lied and a file cut short before it left.
gcloud storage cp --project "$PROJECT" "$uri" "$work/$name" </dev/null >&2 || unable "$uri could not be downloaded."
got="$(stat -c %s "$work/$name")"
[ "$got" -eq "$size" ] || broken "$name is $got bytes here and $size in the bucket."
case "$name" in
*.sql.gz) gzip -t "$work/$name" 2>"$work/gzip.err" || broken "$name is not a readable gzip: $(cat "$work/gzip.err")" ;;
*.dump) head -c 5 "$work/$name" | grep -q '^PGDMP' || broken "$name is not a pg_dump archive." ;;
esac
sum="$(sha256sum "$work/$name" | cut -d' ' -f1)"

# rsync writes to a temporary name and renames on completion, so a copy that
# stops half way is never listed under the export's name.
rsync -e "ssh ${ssh_opts[*]}" "$work/$name" "$host:$dir/$name" >&2 || unable "$name could not be sent to $host."
there="$(remote "sha256sum '$dir/$name'" 2>&1)" || { printf '%s\n' "$there" >&2; unable "$name could not be read back from $host."; }
if [ "${there%% *}" != "$sum" ]; then
    remote "rm -f '$dir/$name'"
    broken "$name arrived on $host with a different checksum, and the copy was removed."
fi

pruned=0
while IFS= read -r old; do
    remote "rm -f '$dir/$old'" && pruned=$((pruned + 1))
done < <(remote "ls -1 '$dir'" | grep -E '^[0-9]{8}T[0-9]{6}\.(sql\.gz|dump)$' | sort | head -n -"$keep")

echo "offsite-backup: copied $name ($size bytes, sha256 ${sum:0:12}) to $host:$dir, removed $pruned older, keeping $keep"
