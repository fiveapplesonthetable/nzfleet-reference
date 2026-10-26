#!/bin/bash
# Whether the newest off-site copy restores and the ledger in it balances.
#
#   ssh nzbackup@devvm 'bash -s' < tools/nzfleet/backup/offsite-restore-check.sh
#
# Runs where the copies are, as the role that owns them, and never sees a
# credential. Loads the newest copy into a database made for the run, checks the
# ledger and the transfers, prints one verdict line and drops the database.
# Exit 0 when it checks out, 1 when it measurably does not, 3 when the check
# could not run. NZELA_OFFSITE_DIR (default /srv/nzbackup) holds the copies and
# NZELA_STUCK_DAYS (default 7) is how long a transfer may sit in a moving status.

set -uo pipefail

dir="${NZELA_OFFSITE_DIR:-/srv/nzbackup}"
stuck_days="${NZELA_STUCK_DAYS:-7}"
[[ "$stuck_days" =~ ^[1-9][0-9]*$ ]] || { echo "offsite-restore-check: UNABLE: NZELA_STUCK_DAYS is whole days." >&2; exit 3; }

unable() { echo "offsite-restore-check: UNABLE: $*"; exit 3; }
broken() { echo "offsite-restore-check: FAILED: $*"; exit 1; }

name="$(ls -1 "$dir" 2>/dev/null | grep -E '^[0-9]{8}T[0-9]{6}\.(sql\.gz|dump)$' | sort | tail -n 1)"
[ -n "$name" ] || unable "there is no copy in $dir."
copy="$dir/$name"

scratch="nzela_restore_$$_$RANDOM"
work="$(mktemp -d -t nzela-restore-check.XXXXXX)"
made=""
cleanup() {
    [ -z "$made" ] || dropdb --if-exists --force "$scratch" >/dev/null 2>&1
    rm -rf "$work"
}
trap cleanup EXIT

q() { psql -X -q -At -v ON_ERROR_STOP=1 -d "$scratch" "$@"; }

# A custom-format archive is read back out as the SQL that gzip would have held.
copy_sql() {
    case "$name" in
    *.dump) pg_restore --no-owner --no-privileges -f - "$copy" ;;
    *) gzip -dc "$copy" ;;
    esac
}

case "$name" in
*.dump) pg_restore --list "$copy" >/dev/null 2>"$work/gzip.err" || broken "$name is not a readable pg_dump archive: $(head -n 3 "$work/gzip.err")" ;;
*) gzip -t "$copy" 2>"$work/gzip.err" || broken "$name is not a readable gzip: $(cat "$work/gzip.err")" ;;
esac
copy_sql | tail -n 5 | grep -q '^-- PostgreSQL database dump complete' \
    || broken "$name ends before pg_dump's completion line, so it is cut off."

began="$SECONDS"
createdb "$scratch" 2>"$work/create.err" || { cat "$work/create.err" >&2; unable "could not create a scratch database."; }
made=1

# The owners and grants in a Cloud SQL export name roles this server does not
# have and nzbackup cannot create. Every other statement still has to succeed.
copy_sql | grep -vE '^(ALTER .* OWNER TO |GRANT |REVOKE |ALTER DEFAULT PRIVILEGES )' |
    psql -X -q -v ON_ERROR_STOP=1 -d "$scratch" >/dev/null 2>"$work/restore.err" \
    || { head -n 20 "$work/restore.err" >&2; broken "$name does not load."; }

[ "$(q -c "select to_regclass('_sqlx_migrations') is not null")" = t ] \
    || broken "$name has no _sqlx_migrations, so it is not a migrated database."
migrations="$(q -c "select count(*) from _sqlx_migrations")"
latest="$(q -c "select coalesce(max(version), 0) from _sqlx_migrations where success")"
[ "$(q -c "select count(*) from _sqlx_migrations where not success")" -eq 0 ] \
    || broken "$name records a migration that did not finish."

# Every group balances to zero in each currency it touches: a dump that lost
# entries still loads, and this is the property that shows it.
unbalanced="$(q -c "select count(*) from (
    select group_id, currency from entries group by group_id, currency
    having sum(case direction when 'debit' then amount_minor else -amount_minor end) <> 0) g")" \
    || broken "the ledger could not be read."
[ "$unbalanced" -eq 0 ] || broken "$unbalanced ledger groups do not balance."
groups="$(q -c "select count(*) from entry_groups")"
[ "$groups" -gt 0 ] || [ -n "${NZELA_ALLOW_EMPTY:-}" ] || broken "the ledger has no entries, so the export restored a schema and no data."

# Judged at the export's own moment, so a copy of a week-old export does not
# call everything stuck. Statuses that wait on a person are not stuck.
stamp="${name%%.*}"
at="${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}+00"
stuck="$(q -c "select count(*) from transfers
    where status in ('QUOTED','COLLECTING','COLLECTED','DISBURSING','REFUNDING')
      and status_changed_at < '$at'::timestamptz - interval '$stuck_days days'")" \
    || broken "the transfers could not be read."
[ "$stuck" -eq 0 ] || broken "$stuck transfers had sat in a moving status for over $stuck_days days when $name was taken."
transfers="$(q -c "select count(*) from transfers")"

echo "offsite-restore-check: PASSED $name: $migrations migrations (latest $latest), $transfers transfers, $groups ledger groups all balanced, none stuck over ${stuck_days}d, in $((SECONDS - began))s"
