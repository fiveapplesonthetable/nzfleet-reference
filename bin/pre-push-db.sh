#!/bin/bash
# Give the hook one migrated SQLx database for the duration of its checks.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ -n "${DATABASE_URL:-}" ]; then
    "$@"
    exit
fi

if ! docker info >/dev/null 2>&1; then
    echo "pre-push: Docker is unavailable and DATABASE_URL is unset." >&2
    echo "  Start Docker, or point DATABASE_URL at a database with every migration applied." >&2
    exit 1
fi

# The daemon chooses a loopback port, while the name keeps concurrent hooks
# from seeing or removing one another's database.
container="nzela-hook-pg-$$-$RANDOM"
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

published="127.0.0.1:${NZELA_HOOK_PG_PORT:-}:5432"
docker_budget=$(PYTHONPATH="$PWD/tools/nzfleet" python3 -m nzfleet.docker_budget --cpus 2 --prefer-owner) || exit 1
read -r -a docker_flags <<<"$docker_budget"
docker run -d "${docker_flags[@]}" --name "$container" -p "$published" \
    --tmpfs /var/lib/postgresql/data:rw,size=4g \
    -e POSTGRES_PASSWORD=nzela -e POSTGRES_DB=nzela_hook \
    postgres:16 -c fsync=off -c synchronous_commit=off \
    -c full_page_writes=off -c max_connections=300 >/dev/null

until docker exec "$container" pg_isready -U postgres -d nzela_hook >/dev/null 2>&1; do
    [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = true ] ||
        { docker logs "$container" >&2; echo "pre-push: Postgres stopped before it was ready." >&2; exit 1; }
    sleep 0.5
done

bound="$(docker port "$container" 5432/tcp)"
port="${bound##*:}"
case "$port" in
    ''|*[!0-9]*) echo "pre-push: Postgres has no published port." >&2; exit 1 ;;
esac
export DATABASE_URL="postgres://postgres:nzela@127.0.0.1:$port/nzela_hook"

docker run --rm -i "${docker_flags[@]}" --network host -v "$PWD:$PWD:ro" -w "$PWD" \
    -e DATABASE_URL postgres:16 bash -e -c '
        for migration in crates/*/migrations/*.sql; do
            psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$migration"
        done
    '
# The image has no python, so the template is made from here through the container's psql.
NZFLEET_PSQL="docker exec -i $container psql" \
    "$(dirname "$0")/nzfleet-migration" template "postgres://postgres:nzela@127.0.0.1:5432/nzela_hook"

"$@"
