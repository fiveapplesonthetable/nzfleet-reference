#!/bin/bash
# What restore-drill.sh promises, against a real dump of a database built from
# this tree's migrations. The newest migration is left out of the dump so the
# roll-forward is exercised too.

set -uo pipefail

command -v docker >/dev/null && docker info >/dev/null 2>&1 || {
    echo "restore-drill-test: docker is not available, and this test restores into a container." >&2
    exit 1
}

here="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$here" || exit 1
work="$(mktemp -d)"
run_id="$(basename "$work")"
src="nzela-restore-drill-test-$run_id"
trap 'docker rm -f "$src" >/dev/null 2>&1; rm -rf "$work"' EXIT
failed=""

fail() { failed="$failed $1"; shift; echo "  $*" >&2; }

psql_src() { docker exec -i "$src" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d source "$@"; }

build_export() {
    docker run -d --name "$src" --network none --tmpfs /var/lib/postgresql/data:rw,size=2g \
        -e POSTGRES_PASSWORD=drill postgres:16-alpine -c fsync=off >/dev/null || return 1
    until docker exec "$src" pg_isready -h 127.0.0.1 -q -U postgres >/dev/null 2>&1; do
        [ "$(docker inspect -f '{{.State.Running}}' "$src" 2>/dev/null)" = true ] || break
        sleep 0.5
    done
    if ! docker exec "$src" pg_isready -h 127.0.0.1 -q -U postgres >/dev/null 2>&1; then
        docker logs "$src" >&2
        echo "restore-drill-test: source Postgres never became ready." >&2
        return 1
    fi
    docker exec "$src" createdb -U postgres source || { docker logs "$src" >&2; return 1; }
    psql_src -c "create table _sqlx_migrations (version bigint primary key, description text not null,
        installed_on timestamptz not null default now(), success boolean not null,
        checksum bytea not null, execution_time bigint not null)" || return 1

    local newest file name version
    newest="$(ls crates/*/migrations/*.sql | xargs -n1 basename | sort | tail -n 1)"
    for file in $(ls crates/*/migrations/*.sql | sort); do
        name="$(basename "$file")"
        [ "$name" != "$newest" ] || continue
        version="$((10#${name%%_*}))"
        psql_src < "$file" || return 1
        psql_src -c "insert into _sqlx_migrations values ($version, 'm', now(), true,
            decode('$(sha384sum "$file" | cut -d' ' -f1)', 'hex'), 0)" || return 1
    done
    docker exec "$src" pg_dump -U postgres source | gzip > "$work/good.sql.gz"
}

drill() {
    output="$(mktemp -d "$work/run.XXXXXX")"
    NZELA_EXPORT_FILE="$1" tools/nzfleet/backup/restore-drill.sh >"$output/out" 2>&1
}

an_invalid_export_is_refused() {
    local property="$1" export="$2" reason="$3" status
    if NZELA_ALLOW_EMPTY=1 drill "$export"; then
        fail "$property" "an invalid export was passed"
        cat "$output/out" >&2
    else
        status=$?
        [ "$status" -eq 1 ] || { fail "$property" "an invalid export returned $status instead of 1"; cat "$output/out" >&2; }
        grep -Fq "$reason" "$output/out" || { fail "$property" "the expected rejection reason was not reported"; cat "$output/out" >&2; }
    fi
}

build_export || { echo "restore-drill-test: could not build an export" >&2; exit 1; }
printf 'drilling a good export\n'

an_export_that_restores_passes() {
    NZELA_ALLOW_EMPTY=1 drill "$work/good.sql.gz" || { fail "$1" "a good export failed"; cat "$output/out" >&2; return; }
    grep -q "PASSED in" "$output/out" || fail "$1" "no verdict was printed"
    grep -F "1 applied on top from this tree" "$output/out" \
        || { fail "$1" "the omitted migration was not applied to the restore"; cat "$output/out" >&2; }
}

an_export_with_no_rows_is_refused() {
    if drill "$work/good.sql.gz"; then
        fail "$1" "a schema with no data was passed"
        cat "$output/out" >&2
    else
        status=$?
        [ "$status" -eq 1 ] || { fail "$1" "an empty export returned $status instead of 1"; cat "$output/out" >&2; }
        grep -Fq "every core table is empty" "$output/out" || { fail "$1" "the export was refused for another reason"; cat "$output/out" >&2; }
    fi
}

a_truncated_export_is_refused() {
    head -c 2000 "$work/good.sql.gz" > "$work/cut.sql.gz"
    an_invalid_export_is_refused "$1" "$work/cut.sql.gz" "is not a readable gzip"
}

a_dump_cut_at_a_line_is_refused_though_it_is_valid_gzip() {
    gzip -dc "$work/good.sql.gz" | head -n -20 | gzip > "$work/short.sql.gz"
    an_invalid_export_is_refused "$1" "$work/short.sql.gz" "ends before pg_dump's completion line"
}

a_migration_edited_after_the_export_is_refused() {
    local first version
    first="$(ls crates/*/migrations/*.sql | sort | head -n 1)"
    version="$((10#$(basename "$first" | cut -d_ -f1)))"
    docker exec "$src" psql -X -q -U postgres -d source -c "update _sqlx_migrations set checksum = '\\x00' where version = $version" || return
    docker exec "$src" pg_dump -U postgres source | gzip > "$work/edited.sql.gz"
    an_invalid_export_is_refused "$1" "$work/edited.sql.gz" "migration $version was edited after the export applied it"
}

a_ledger_group_that_does_not_balance_is_refused() {
    psql_src -c "alter table entry_groups drop constraint entry_groups_transfer_id_fkey" \
        || { fail "$1" "the foreign key could not be dropped"; return; }
    psql_src -c "set session_replication_role = replica;
        insert into entry_groups (id, transfer_id, memo) values ('00000000-0000-7000-8000-000000000001', gen_random_uuid(), 'x');
        insert into entries (group_id, account, direction, amount_minor, currency, memo)
            values ('00000000-0000-7000-8000-000000000001', 'float.collect.mock.cdf', 'debit', 5, 'CDF', 'x')" \
        || { fail "$1" "the unbalanced group could not be written"; return; }
    docker exec "$src" pg_dump -U postgres source | gzip > "$work/unbalanced.sql.gz"
    an_invalid_export_is_refused "$1" "$work/unbalanced.sql.gz" "ledger groups do not balance"
}

an_unreadable_source_is_not_a_finding() {
    if drill "$work/missing.sql.gz"; then
        fail "$1" "a missing file was called a failed restore"
    else
        status=$?
        [ "$status" -eq 3 ] || { fail "$1" "a missing file returned $status instead of 3"; cat "$output/out" >&2; }
    fi
}

for property in \
    an_export_that_restores_passes \
    an_export_with_no_rows_is_refused \
    a_truncated_export_is_refused \
    a_dump_cut_at_a_line_is_refused_though_it_is_valid_gzip \
    a_ledger_group_that_does_not_balance_is_refused \
    a_migration_edited_after_the_export_is_refused \
    an_unreadable_source_is_not_a_finding
do
    echo "$property"
    "$property" "$property"
done

if [ -n "$failed" ]; then
    printf '\nfailed:%s\n' "$failed" >&2
    exit 1
fi

echo "restore-drill-test: all cases passed"
