#!/bin/bash
# Whether the newest database export restores, and into what.
#
#   PROJECT=... BUCKET=... tools/nzfleet/backup/restore-drill.sh
#   NZELA_EXPORT_FILE=export.sql.gz tools/nzfleet/backup/restore-drill.sh
#
# Downloads the newest export, loads it into a Postgres 16 in a docker
# container that exists for the run, holds the restored migration history to
# the files in this tree, rolls the migrations the tree has and the export does
# not onto it, and counts the core tables. Docker and not a temporary Cloud SQL
# instance: it costs nothing, needs no grant beyond reading the bucket, and
# starts in seconds. What it cannot see is a restore that only fails on Cloud
# SQL's own import path, which is the price of not paying for an instance weekly.
#
# The live database is never named. The only credential used reads the export
# bucket; the target is a tmpfs-backed container with no published port.
#
# Exit 0 when the export restores and checks out, 1 when it measurably does not,
# 3 when the drill could not run (no docker, bucket unreadable, no export), which
# says nothing about the backups, as in backup-fresh.sh. NZELA_EXPORT_FILE skips
# the download and drills that file.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$root" || exit 3

IMAGE="${NZELA_DRILL_IMAGE:-postgres:16-alpine}"
CORE_TABLES=(senders sender_numbers transfers entry_groups entries events operators rates)

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
said() {
    printf '%s\n' "$*"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
}
unable() { said "restore-drill: $*"; exit 3; }
broken() { said "restore-drill: FAILED: $*"; exit 1; }

work="$(mktemp -d -t nzela-restore-drill.XXXXXX)"
run_id="${work##*.}"
box="nzela-restore-drill-$run_id"
cleanup() {
    docker rm -f "$box" >/dev/null 2>&1
    rm -rf "$work"
}
trap cleanup EXIT

command -v docker >/dev/null && docker info >/dev/null 2>&1 || unable "docker is not available, so there is nowhere to restore to."

# The export as the SQL that loads it, whichever way it was written: a custom
# format archive is turned back into text by pg_restore, a Cloud SQL export is
# gzipped text already. The archive is read by the drill's own image, so its
# text is never newer than the server it loads into.
export_sql() {
    case "$export_name" in
    *.dump) docker run --rm -i --network none -u "$(id -u)" -v "$export_path:/export.dump:ro" "$IMAGE" \
        pg_restore --no-owner --no-privileges -f - /export.dump ;;
    *) gzip -dc "$export_path" ;;
    esac
}

# The newest export's object URI, read off the name as backup-fresh.sh does.
newest_export() {
    local listing
    listing="$(gcloud storage ls --project "$PROJECT" "gs://$BUCKET/nzela/" </dev/null 2>&1)" || {
        printf '%s\n' "$listing" >&2
        return 1
    }
    printf '%s\n' "$listing" | grep -E '/nzela/[0-9]{8}T[0-9]{6}\.(sql\.gz|dump)$' | sort | tail -n 1
}

fetch_export() {
    if [ -n "${NZELA_EXPORT_FILE:-}" ]; then
        [ -s "$NZELA_EXPORT_FILE" ] || unable "$NZELA_EXPORT_FILE is missing or empty."
        export_name="$(basename "$NZELA_EXPORT_FILE")"
        export_path="$NZELA_EXPORT_FILE"
        return
    fi
    [ -n "${PROJECT:-}" ] && [ -n "${BUCKET:-}" ] || unable "PROJECT and BUCKET are unset."
    local uri
    uri="$(newest_export)" || unable "the export bucket could not be read."
    [ -n "$uri" ] || broken "the bucket holds no export."
    export_name="$(basename "$uri")"
    export_path="$work/$export_name"
    gcloud storage cp --project "$PROJECT" "$uri" "$export_path" </dev/null >&2 \
        || unable "$uri could not be downloaded."
}

# psql in the container as its superuser, against the one database in it.
psql_in() { docker exec -i "$box" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d restored "$@"; }

start_postgres() {
    docker run -d --name "$box" --network none --tmpfs /var/lib/postgresql/data:rw,size=4g \
        -e POSTGRES_PASSWORD=drill "$IMAGE" -c fsync=off -c synchronous_commit=off \
        -c full_page_writes=off >/dev/null || unable "the throwaway Postgres did not start."
    # The image starts a temporary socket-only server during initdb; TCP waits for the real server.
    until docker exec "$box" pg_isready -h 127.0.0.1 -q -U postgres >/dev/null 2>&1; do
        [ "$(docker inspect -f '{{.State.Running}}' "$box" 2>/dev/null)" = true ] || break
        sleep 0.5
    done
    if ! docker exec "$box" pg_isready -h 127.0.0.1 -q -U postgres >/dev/null 2>&1; then
        docker logs "$box" >&2
        unable "the throwaway Postgres never became ready."
    fi
    docker exec "$box" createdb -U postgres restored || {
        docker logs "$box" >&2
        unable "could not create the target database."
    }
}

# A Cloud SQL export names the roles that owned and were granted things, and
# none of them exist in a fresh server. Creating them is not forgiving the dump:
# every statement in it still has to succeed.
create_referenced_roles() {
    local role
    export_sql |
        sed -nE 's/.* OWNER TO ("?[A-Za-z0-9_]+"?);.*/\1/p; s/^GRANT .* TO ("?[A-Za-z0-9_]+"?)( WITH .*)?;.*/\1/p' |
        sort -u | { grep -vxiE 'public|postgres' || true; } |
        while IFS= read -r role; do
            echo "create role $role nologin;"
        done | docker exec -i "$box" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d restored
}

# Judged before a container exists, so a cut-off export is refused whether or
# not the machine could have started Postgres. pg_dump writes its completion
# line last, which catches a file cut at a point gzip itself accepts.
check_export_is_whole() {
    case "$export_name" in
    *.dump) ;;
    *) gzip -t "$export_path" 2>"$work/gzip.err" || broken "$export_name is not a readable gzip: $(cat "$work/gzip.err")" ;;
    esac
    export_sql | tail -n 5 | grep -q '^-- PostgreSQL database dump complete' \
        || broken "$export_name ends before pg_dump's completion line, so it is cut off."
}

restore() {
    create_referenced_roles || broken "the roles $export_name refers to could not be created."
    export_sql | psql_in >"$work/restore.out" 2>"$work/restore.err" \
        || { head -n 20 "$work/restore.err" >&2; broken "$export_name does not load."; }
}

# The version and file checksum sqlx records for each migration it applied.
# sqlx's checksum is the SHA-384 of the file, so a migration edited after it
# shipped, which a database that was migrated before would never notice, shows.
check_migrations() {
    local file name version sum recorded
    local -A on_disk=() sums=()
    for file in crates/*/migrations/*.sql; do
        name="$(basename "$file")"
        version="$((10#${name%%_*}))"
        on_disk[$version]="$file"
        sums[$version]="$(sha384sum "$file" | cut -d' ' -f1)"
    done

    psql_in -Atc "select to_regclass('_sqlx_migrations') is not null" | grep -qx t \
        || broken "the export has no _sqlx_migrations, so it is not a migrated database."

    local -a pending=()
    while IFS='|' read -r version recorded; do
        [ -n "${on_disk[$version]:-}" ] || broken "the export has migration $version, which this tree does not."
        [ "${sums[$version]}" = "$recorded" ] || broken "migration $version was edited after the export applied it: ${on_disk[$version]}."
    done < <(psql_in -Atc "select version, encode(checksum, 'hex') from _sqlx_migrations order by version" </dev/null)

    applied="$(psql_in -Atc "select count(*) from _sqlx_migrations")"
    while read -r version; do
        psql_in -Atc "select 1 from _sqlx_migrations where version = $version" </dev/null | grep -qx 1 \
            || pending+=("$version")
    done < <(printf '%s\n' "${!on_disk[@]}" | sort -n)

    # Migrations merged since the export are applied to the copy: the next
    # deploy does exactly this to the live database, and a restore that cannot
    # be rolled forward is not a way back.
    for version in "${pending[@]}"; do
        psql_in < "${on_disk[$version]}" >/dev/null 2>"$work/migrate.err" \
            || { head -n 20 "$work/migrate.err" >&2; broken "migration $version does not apply to the restored database."; }
    done
    rolled="${#pending[@]}"
}

# `reltuples` is an estimate and a restored database has not been analyzed, so
# the rows are counted.
count_core_tables() {
    local table n nonempty=0
    for table in "${CORE_TABLES[@]}"; do
        n="$(psql_in -Atc "select count(*) from $table" 2>/dev/null)" \
            || broken "core table $table is missing from the restored database."
        printf '  %-16s %s\n' "$table" "$n"
        [ "$n" -eq 0 ] || nonempty=1
    done
    [ "$nonempty" -eq 1 ] || [ -n "${NZELA_ALLOW_EMPTY:-}" ] \
        || { broken "every core table is empty, so the export restored a schema and no data." >&2; }
}

# Every group balances to zero in each currency it touches, which is the one
# property of the ledger a restore can silently lose: a dump that dropped
# entries still loads. Counted in the restored copy, after migrations.
check_ledger() {
    local unbalanced
    unbalanced="$(psql_in -Atc "select count(*) from (
        select group_id, currency from entries group by group_id, currency
        having sum(case direction when 'debit' then amount_minor else -amount_minor end) <> 0) g")" \
        || broken "the ledger could not be read in the restored database."
    [ "$unbalanced" -eq 0 ] || broken "$unbalanced ledger groups do not balance in the restored database."
    ledger_groups="$(psql_in -Atc "select count(*) from entry_groups")"
}

began="$SECONDS"
fetch_export
check_export_is_whole
start_postgres
restore
check_migrations
said "restore-drill: $export_name restored; $applied migrations in the export, $rolled applied on top from this tree"
check_ledger
count_core_tables > "$work/counts"
printf '  %-16s %s groups, all balanced\n' ledger "$ledger_groups" >> "$work/counts"
cat "$work/counts"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || cat "$work/counts" >> "$GITHUB_STEP_SUMMARY"
said "restore-drill: PASSED in $((SECONDS - began)) seconds"
