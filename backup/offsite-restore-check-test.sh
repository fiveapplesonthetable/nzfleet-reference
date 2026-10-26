#!/bin/bash
# What offsite-restore-check.sh promises, against dumps of a database built from
# this tree's migrations. psql, createdb and dropdb are stand-ins that run in a
# Postgres container, as nzbackup's own would run against devvm's server.

set -uo pipefail

command -v docker >/dev/null && docker info >/dev/null 2>&1 || {
    echo "offsite-restore-check-test: docker is not available, and this test restores into a container." >&2
    exit 1
}

here="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$here" || exit 1
work="$(mktemp -d)"
pg="nzela-offsite-check-test-$(basename "$work")"
trap 'docker rm -f "$pg" >/dev/null 2>&1; rm -rf "$work"' EXIT
failed=""
mkdir -p "$work/bin" "$work/copies"

fail() { failed="$failed $1"; shift; echo "  $*" >&2; }

for tool in psql createdb dropdb; do
    cat > "$work/bin/$tool" <<STUB
#!/bin/bash
exec docker exec -i "$pg" $tool -U postgres "\$@"
STUB
done
chmod +x "$work/bin/"*

docker run -d --name "$pg" --network none --tmpfs /var/lib/postgresql/data:rw,size=2g \
    -e POSTGRES_PASSWORD=drill postgres:16-alpine -c fsync=off >/dev/null || exit 1
until docker exec "$pg" pg_isready -h 127.0.0.1 -q -U postgres >/dev/null 2>&1; do
    [ "$(docker inspect -f '{{.State.Running}}' "$pg" 2>/dev/null)" = true ] || { docker logs "$pg" >&2; exit 1; }
    sleep 0.5
done
docker exec "$pg" createdb -U postgres source || exit 1
docker exec "$pg" psql -X -q -U postgres -c "create role cloudsqlsuperuser nologin" >/dev/null || exit 1

src() { docker exec -i "$pg" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d source "$@"; }

# A sender, a disbursed transfer and a balanced ledger group, on the real schema.
build_source() {
    src -c "create table _sqlx_migrations (version bigint primary key, description text not null,
        installed_on timestamptz not null default now(), success boolean not null,
        checksum bytea not null, execution_time bigint not null)" || return 1
    local file version
    for file in $(ls crates/*/migrations/*.sql | sort); do
        src < "$file" || return 1
        version="$((10#$(basename "$file" | cut -d_ -f1)))"
        src -c "insert into _sqlx_migrations values ($version, 'm', now(), true, '\\x00', 0)" || return 1
    done
    src -c "insert into senders (id, google_subject, google_email, google_full_name)
            values ('00000000-0000-7000-8000-0000000000a1', 's', 'a@example.com', 'A')" || return 1
    add_transfer 00000000-0000-7000-8000-0000000000b1 DISBURSED '2026-10-01 00:00:00+00'
    src -c "insert into entry_groups (id, transfer_id, memo)
            values ('00000000-0000-7000-8000-0000000000c1', '00000000-0000-7000-8000-0000000000b1', 'x');
        insert into entries (group_id, account, direction, amount_minor, currency, memo) values
            ('00000000-0000-7000-8000-0000000000c1', 'float.collect.mock.cdf', 'debit', 5, 'CDF', 'x'),
            ('00000000-0000-7000-8000-0000000000c1', 'float.payout.mock.cdf', 'credit', 5, 'CDF', 'x')"
}

add_transfer() {
    src -c "insert into transfers (id, sender_id, corridor, status, collect_minor, collect_currency,
            payout_minor, payout_currency, fee_minor, mid_rate_micro, customer_rate_micro, sender_msisdn,
            recipient_msisdn, instrument, expires_at, recipient_operator, status_changed_at)
        values ('$1', '00000000-0000-7000-8000-0000000000a1', 'cd-cdf-ci-xof', '$2', 1000, 'CDF', 5, 'XOF',
            10, 100, 100, '+243000', '+225000', 'wallet', '$3', 'wave_ci', '$3')"
}

# Named as an export is, so the newest one is read off the name.
dump_as() {
    docker exec "$pg" pg_dump -U postgres source | gzip > "$work/copies/$1.sql.gz"
}

check() {
    NZELA_OFFSITE_DIR="$work/copies" PATH="$work/bin:$PATH" tools/nzfleet/backup/offsite-restore-check.sh >"$work/out" 2>&1
}

build_source || { echo "offsite-restore-check-test: could not build a source database" >&2; exit 1; }
dump_as 20261001T061700
echo "checking a good copy"

a_copy_that_restores_passes() {
    check || { fail "$1" "a good copy failed"; cat "$work/out" >&2; return; }
    grep -q "PASSED 20261001T061700" "$work/out" || fail "$1" "no verdict was printed"
    [ "$(wc -l < "$work/out")" -eq 1 ] || fail "$1" "the verdict is not one line"
}

the_scratch_database_is_dropped() {
    check
    [ "$(docker exec "$pg" psql -U postgres -Atc "select count(*) from pg_database where datname like 'nzela_restore_%'")" = 0 ] \
        || fail "$1" "a scratch database outlived the run"
}

the_newest_copy_is_the_one_checked() {
    dump_as 20260901T061700
    check
    grep -q "PASSED 20261001T061700" "$work/out" || fail "$1" "an older copy was checked"
}

a_ledger_group_that_does_not_balance_is_refused() {
    src -c "set session_replication_role = replica;
        insert into entry_groups (id, transfer_id, memo)
            values ('00000000-0000-7000-8000-0000000000c2', '00000000-0000-7000-8000-0000000000b1', 'x');
        insert into entries (group_id, account, direction, amount_minor, currency, memo)
            values ('00000000-0000-7000-8000-0000000000c2', 'float.collect.mock.cdf', 'debit', 5, 'CDF', 'x')" \
        || { fail "$1" "could not write the unbalanced group"; return; }
    dump_as 20261002T061700
    check && fail "$1" "an unbalanced ledger passed"
    grep -q "FAILED: 1 ledger groups do not balance" "$work/out" || { fail "$1" "refused for another reason"; cat "$work/out" >&2; }
    rm "$work/copies/20261002T061700.sql.gz"
}

a_transfer_stuck_when_the_export_was_taken_is_refused() {
    add_transfer 00000000-0000-7000-8000-0000000000b2 COLLECTING '2026-09-01 00:00:00+00' \
        || { fail "$1" "could not write the stuck transfer"; return; }
    dump_as 20261003T061700
    check && fail "$1" "a stuck transfer passed"
    grep -q "FAILED: 1 transfers had sat in a moving status" "$work/out" || { fail "$1" "refused for another reason"; cat "$work/out" >&2; }
    rm "$work/copies/20261003T061700.sql.gz"
}

a_transfer_that_is_young_for_its_export_is_not_stuck() {
    src -c "delete from transfers where id = '00000000-0000-7000-8000-0000000000b2'" >/dev/null 2>&1
    add_transfer 00000000-0000-7000-8000-0000000000b3 COLLECTING '2026-10-04 00:00:00+00'
    dump_as 20261005T061700
    check || { fail "$1" "a recent moving transfer was called stuck"; cat "$work/out" >&2; }
    rm "$work/copies/20261005T061700.sql.gz"
}

a_dump_cut_at_a_line_is_refused_though_it_is_valid_gzip() {
    gzip -dc "$work/copies/20261001T061700.sql.gz" | head -n -20 | gzip > "$work/copies/20261009T061700.sql.gz"
    check && fail "$1" "a cut dump passed"
    grep -q "ends before pg_dump's completion line" "$work/out" || { fail "$1" "refused for another reason"; cat "$work/out" >&2; }
    rm "$work/copies/20261009T061700.sql.gz"
}

no_copy_is_not_a_finding_about_the_backups() {
    mkdir "$work/empty"
    NZELA_OFFSITE_DIR="$work/empty" PATH="$work/bin:$PATH" tools/nzfleet/backup/offsite-restore-check.sh >"$work/out" 2>&1
    [ $? = 3 ] || fail "$1" "an empty directory did not exit 3"
}

for property in \
    a_copy_that_restores_passes \
    the_scratch_database_is_dropped \
    the_newest_copy_is_the_one_checked \
    a_transfer_stuck_when_the_export_was_taken_is_refused \
    a_transfer_that_is_young_for_its_export_is_not_stuck \
    a_dump_cut_at_a_line_is_refused_though_it_is_valid_gzip \
    no_copy_is_not_a_finding_about_the_backups \
    a_ledger_group_that_does_not_balance_is_refused
do
    echo "$property"
    "$property" "$property"
done

[ -z "$failed" ] || { echo "failed:$failed" >&2; exit 1; }
