#!/usr/bin/env bash
#
# The local stack the real app is driven against, and the phones it is driven on.
#
#   tools/nzfleet/devloop/device-lab.sh up        the stack: database, API, sweeper, stand-in, forwarder
#   tools/nzfleet/devloop/device-lab.sh devices   build the debug app, install it, and point each handset at the stack
#   tools/nzfleet/devloop/device-lab.sh reset     the seeded world again, in about a second
#   tools/nzfleet/devloop/device-lab.sh sweep     one sweeper pass, for a stack started with a manual tick
#   tools/nzfleet/devloop/device-lab.sh status    what is running, and which handsets can reach it
#   tools/nzfleet/devloop/device-lab.sh down      stop it and drop the databases
#
# docs/device-lab.md says what each piece is and how the host forwards to it.
#
# DATABASE_URL names a Postgres whose login may create databases and roles; the
# lab keeps its own databases beside whatever else is there. The binaries are the
# debug build in cargo's target directory unless NZELA_BIN names another.
#
#   DEVICE_LAB_PORT         the port handsets reach, on the host and on this machine (8080)
#   DEVICE_LAB_SWEEP_TICK   seconds between sweeper passes, or "manual" (2)
#   DEVICE_LAB_ADB          the adb server the phones are on (tcp:192.168.122.1:5037)
#   DEVICE_LAB_BIND         the address the forwarder listens on (0.0.0.0)
#   DEVICE_LAB_SERIAL       use only this handset; every other one on the server is left alone
#   DEVICE_LAB_PACKAGE      the app's package, the only one the lab force-stops, uninstalls or installs (app.nzela)
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
ROOT="$PWD"
# shellcheck source=lib/stack.sh
. scripts/lib/stack.sh

PORT="${DEVICE_LAB_PORT:-8080}"
API_PORT=$((PORT + 1))
STANDIN_PORT=$((PORT + 2))
SWEEP_PORT=$((PORT + 3))
TICK="${DEVICE_LAB_SWEEP_TICK:-2}"
BIND="${DEVICE_LAB_BIND:-0.0.0.0}"
PHONES_ADB="${DEVICE_LAB_ADB:-tcp:192.168.122.1:5037}"
# The port the app's development sign-in already defaults to.
DEVICE_PORT=8080
APP_PACKAGE="${DEVICE_LAB_PACKAGE:-app.nzela}"
ONLY_SERIAL="${DEVICE_LAB_SERIAL:-}"

BIN="${NZELA_BIN:-${CARGO_TARGET_DIR:-$ROOT/target}/debug}"
LAB="${DEVICE_LAB_DIR:-${CARGO_TARGET_DIR:-$ROOT/target}/device-lab-$PORT}"
LOGS="$LAB/logs"
LIVE_DB="nzela_lab_$PORT"
SEED_DB="nzela_lab_seed_$PORT"
RUNTIME_ROLE="nzela_lab_app_$PORT"

STANDIN="http://127.0.0.1:$STANDIN_PORT"
API="http://127.0.0.1:$API_PORT"
SWEEPER="http://127.0.0.1:$SWEEP_PORT"
CALLER=scheduler@lab.iam.gserviceaccount.com
AUDIENCE="$SWEEPER/sweep"

note() { printf '%s\n' "$*"; }
fail() { printf 'device-lab: %s\n' "$*" >&2; exit 1; }
sql() { psql "$E2E_URL" -v ON_ERROR_STOP=1 -Atqc "$1"; }
admin_sql() { PGOPTIONS='-c client_min_messages=warning' psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -Atqc "$1"; }

need() {
    local tool
    for tool in psql curl python3; do
        command -v "$tool" >/dev/null 2>&1 || fail "$tool is not on PATH"
    done
    [ -n "${DATABASE_URL:-}" ] || fail "DATABASE_URL is unset"
    local bin
    for bin in nzela nzstandin; do
        [ -x "$BIN/$bin" ] || fail "$BIN/$bin is not built: cargo build --bin nzela --bin nzstandin"
    done
}

db_url() { printf '%s/%s' "${DATABASE_URL%/*}" "$1"; }

# The runtime login owns nothing, as a deployment's does. Its password is made
# once and kept, because a database cloned from the template has to be served
# by the login the template granted.
runtime_url() {
    local password
    if [ ! -s "$LAB/runtime-password" ]; then
        mkdir -p "$LAB"
        python3 -c 'import secrets;print(secrets.token_hex(16))' >"$LAB/runtime-password"
        chmod 600 "$LAB/runtime-password"
    fi
    password="$(cat "$LAB/runtime-password")"
    python3 - "$(db_url "$1")" "$RUNTIME_ROLE" "$password" <<'EOF'
import sys, urllib.parse
url = urllib.parse.urlsplit(sys.argv[1])
host = url.netloc.rpartition("@")[2]
print(url._replace(netloc=f"{sys.argv[2]}:{sys.argv[3]}@{host}").geturl())
EOF
}

# ---- processes ----

pidfile() { printf '%s/%s.pid' "$LAB" "$1"; }
alive() { local pid; pid="$(cat "$(pidfile "$1")" 2>/dev/null)" && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; }

# Detached, so the stack outlives the shell that started it.
launch() {
    local name=$1; shift
    setsid "$@" >"$LOGS/$name.log" 2>&1 </dev/null &
    echo $! >"$(pidfile "$name")"
    disown
}

stop() {
    local pid
    pid="$(cat "$(pidfile "$1")" 2>/dev/null)" || return 0
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        # Killed outright: a graceful drain is a second the lab spends on every
        # reset, and nothing here has a request worth finishing.
        kill -KILL "$pid" 2>/dev/null
        # Not `tail --pid`: in a container with no init the killed process stays a
        # zombie nobody reaps, and a zombie still answers kill -0, so that waited forever.
        while [ -e "/proc/$pid" ] && [ "$(cut -d' ' -f3 "/proc/$pid/stat" 2>/dev/null)" != Z ]; do :; done
    fi
    rm -f "$(pidfile "$1")"
}

# What the service and the sweeper share, against the database named.
environment() {
    local database=$1
    RUNTIME_URL="$(runtime_url "$database")"
    stack_environment_cached
    SHARED+=(
        # A scenario taps through a journey far faster than a person does.
        REQUESTS_PER_MINUTE=100000
        PROMPTS_PER_NUMBER_PER_HOUR=1000
    )
}

# Kept across a reset: it holds no state yet, and starting it costs a second of
# key generation.
# `nzela settings` is most of a second and only changes with the binary.
stack_environment_cached() {
    local key
    key="$(stat -c %Y "$BIN/nzela")-$STANDIN"
    if [ "$(head -n1 "$LAB/shared.env" 2>/dev/null)" != "$key" ]; then
        stack_environment
        { echo "$key"; printf '%s\n' "${SHARED[@]:1}"; } >"$LAB/shared.env"
    else
        local rest
        mapfile -t rest < <(tail -n +2 "$LAB/shared.env")
        SHARED=("DATABASE_URL=$RUNTIME_URL" "${rest[@]}")
    fi
}

start_standin() {
    alive standin && return 0
    launch standin "$BIN/nzstandin" --listen "127.0.0.1:$STANDIN_PORT"
    await_url "$STANDIN/healthz" "the stand-in" alive standin
}

start_service() {
    local database=$1
    environment "$database"
    # Loopback, because the development sign-in refuses to be mounted anywhere
    # else; the forwarder is what a handset reaches.
    launch api env "${SHARED[@]}" "LISTEN_ADDR=127.0.0.1:$API_PORT" DEVELOPMENT_SIGN_IN=true \
        GOOGLE_CLIENT_ID=lab.apps.googleusercontent.com \
        MAX_TRANSFER_CDF_MINOR=230000000 MAX_TRANSFER_USD_MINOR=100000 \
        "$BIN/nzela" serve
    launch sweeper env "${SHARED[@]}" "LISTEN_ADDR=127.0.0.1:$SWEEP_PORT" \
        "SWEEP_CALLER=$CALLER" "SWEEP_AUDIENCE=$AUDIENCE" "$BIN/nzela" sweeper
    await_url "$API/health" "the service" alive api
    await_url "$SWEEPER/health" "the sweeper" alive sweeper
}

start_forwarder() {
    alive forwarder && return 0
    if ss -ltn "sport = :$PORT" | grep -q LISTEN; then
        fail "port $PORT is taken by something that is not this lab's forwarder"
    fi
    launch forwarder python3 tools/nzfleet/devloop/forward.py "$BIND" "$PORT" 127.0.0.1 "$API_PORT"
    await_url "http://127.0.0.1:$PORT/health" "the forwarder" alive forwarder
}

start_ticker() {
    alive ticker && return 0
    [ "$TICK" = manual ] && return 0
    DEVICE_LAB_SWEEP_TICK="$TICK" launch ticker "$0" tick
}

stop_services() {
    stop sweeper
    stop api
}

sweep_once() {
    local token
    token="$(curl -sf "$STANDIN/token?email=$CALLER&aud=$AUDIENCE")" || return 1
    curl -sf -X POST "$SWEEPER/sweep" -H "Authorization: Bearer $token"
}

# ---- the seed ----

# The template is the world every scenario starts in: migrated, priced, with an
# operator, a funded payout pool and one verified sender. It is made by the
# service's own routes so that nothing in it is a state the service cannot reach.
stamp() {
    cat crates/*/migrations/*.sql deploy/seed-dev.sql tools/nzfleet/devloop/device-lab.sh scripts/lib/stack.sh | sha256sum | cut -d' ' -f1
}

template_current() {
    [ "$(cat "$LAB/template.stamp" 2>/dev/null)" = "$(stamp)" ] &&
        [ -s "$LAB/operator-token" ] &&
        [ "$(admin_sql "select count(*) from pg_database where datname = '$SEED_DB'")" = 1 ]
}

drop_database() {
    admin_sql "select pg_terminate_backend(pid) from pg_stat_activity where datname = '$1'" >/dev/null
    admin_sql "drop database if exists $1"
}

build_template() {
    note "building the seed database $SEED_DB"
    stop_services
    stop standin
    drop_database "$LIVE_DB"
    drop_database "$SEED_DB"
    DB="$SEED_DB"
    E2E_URL="$(db_url "$SEED_DB")"
    RUNTIME_URL="$(runtime_url "$SEED_DB")"
    stack_database

    local operator_token sha
    operator_token="lab-$(python3 -c 'import secrets;print(secrets.token_hex(24))')"
    sha="$(printf '%s' "$operator_token" | sha256sum | cut -d' ' -f1)"
    sql "insert into operator_sessions (token_sha256, operator_email, expires_at)
         values (decode('$sha', 'hex'), 'ops@nzela.example', now() + interval '10 years')" >/dev/null
    printf '%s' "$operator_token" >"$LAB/operator-token"
    chmod 600 "$LAB/operator-token"

    start_standin
    start_service "$SEED_DB"
    local admin=(-H "Authorization: Bearer $operator_token") json=(-H 'content-type: application/json')
    local code sender
    code="$(curl -sS -o "$LOGS/seed.json" -w '%{http_code}' -X POST "$API/v1/admin/ledger/float" "${admin[@]}" "${json[@]}" \
        -d '{"provider":"honeycoin","pool":"payout","amount_minor":100000000,"currency":"XOF","reference":"lab-float","note":"the lab pool"}')"
    [ "$code" = 201 ] || { cat "$LOGS/seed.json" >&2; fail "funding the payout pool answered $code"; }
    code="$(curl -sS -o "$LOGS/seed.json" -w '%{http_code}' -X POST "$API/v1/sessions/development" "${json[@]}" \
        -d '{"email":"amina@example.com","full_name":"Amina Kabila"}')"
    [ "$code" = 201 ] || { cat "$LOGS/seed.json" >&2; fail "signing the seeded sender in answered $code"; }
    sender="$(sql "select id from senders where google_email = 'amina@example.com'")"
    code="$(curl -sS -o "$LOGS/seed.json" -w '%{http_code}' -X POST "$API/v1/admin/senders/$sender/verification" "${admin[@]}" "${json[@]}" \
        -d '{"status":"VERIFIED","note":"the lab sender","acknowledged_risk":true}')"
    [ "$code" = 204 ] || { cat "$LOGS/seed.json" >&2; fail "verifying the seeded sender answered $code"; }
    # The session the sign-in opened is the app's to make; the next one is.
    sql "delete from sessions" >/dev/null
    stop_services
    admin_sql "select pg_terminate_backend(pid) from pg_stat_activity where datname = '$SEED_DB'" >/dev/null
    stamp >"$LAB/template.stamp"
}

# A copy of the template under the live name, with the clock-dependent parts of
# the seed made current: a rate older than RATE_MAX_AGE quotes nothing.
clone_world() {
    drop_database "$LIVE_DB"
    admin_sql "create database $LIVE_DB template $SEED_DB"
    E2E_URL="$(db_url "$LIVE_DB")"
    psql "$E2E_URL" -v ON_ERROR_STOP=1 -q -f deploy/seed-dev.sql || fail "seed-dev.sql failed"
}

# ---- devices ----

adb_phones() { ADB_SERVER_SOCKET="$PHONES_ADB" adb "$@"; }
adb_local() { env -u ADB_SERVER_SOCKET adb "$@"; }

serials_of() {
    "$@" devices 2>/dev/null | awk -v only="$ONLY_SERIAL" 'NR>1 && $2=="device" && (only=="" || $1==only) {print $1}'
}

# Prints "<kind> <serial>" for every handset the lab will use: each phone on the
# host's adb server, or the emulator when there is none.
handsets() {
    local serial found=0
    while read -r serial; do
        [ -n "$serial" ] || continue
        echo "phone $serial"
        found=1
    done < <(serials_of adb_phones)
    [ "$found" = 1 ] && return 0
    [ -z "$ONLY_SERIAL" ] || return 1
    serial="$(env -u ADB_SERVER_SOCKET scripts/device.sh select 2>/dev/null)" || return 1
    echo "emulator $serial"
}

run_adb() { # run_adb <kind> <adb args...>
    local kind=$1; shift
    # adb shell reads stdin, which here is the list of handsets being walked.
    if [ "$kind" = phone ]; then adb_phones "$@" </dev/null; else adb_local "$@" </dev/null; fi
}

# From the handset's own side: does its loopback reach the API through the
# reverse, the host and the forwarder.
reaches_api() { # reaches_api <kind> <serial>
    # toybox nc quits when its input ends, so the request is held open long
    # enough for the reply to come back.
    run_adb "$1" -s "$2" shell "{ printf 'GET /health HTTP/1.0\r\n\r\n'; sleep 1; } | nc -w 3 127.0.0.1 $DEVICE_PORT" 2>/dev/null |
        grep -q '^HTTP/1.[01] 200'
}

attach() { # attach <kind> <serial>
    local kind=$1 serial=$2
    # The only listener of ours a handset can hold on this port is the data-layer
    # page's stand-in, so stopping the app frees it.
    run_adb "$kind" -s "$serial" shell am force-stop "$APP_PACKAGE" >/dev/null 2>&1
    run_adb "$kind" -s "$serial" reverse --remove "tcp:$DEVICE_PORT" >/dev/null 2>&1
    run_adb "$kind" -s "$serial" reverse "tcp:$DEVICE_PORT" "tcp:$PORT" >/dev/null ||
        fail "$serial would not take a reverse on $DEVICE_PORT"
    if reaches_api "$kind" "$serial"; then
        note "  $kind $serial: localhost:$DEVICE_PORT reaches the API"
    else
        note "  $kind $serial: reversed to host port $PORT, which does not reach the API; the host forwarder is not running (docs/device-lab.md)"
        return 1
    fi
}

build_app() {
    (cd frontend/android && /srv/fleet/bin/gradle-slot ./gradlew --console=plain -q :app:assembleDebug) ||
        fail "the debug app did not build"
}

cmd_devices() {
    local kind serial apk=frontend/android/app/build/outputs/apk/debug/app-debug.apk status=0 any=0
    build_app
    while read -r kind serial; do
        [ -n "$kind" ] || continue
        any=1
        note "$kind $serial"
        scripts/device.sh prepare "$serial" >/dev/null 2>&1
        # Uninstalled first: a handset may hold a newer or differently signed
        # build, which `install -r` refuses, and the run wants empty data anyway.
        run_adb "$kind" -s "$serial" uninstall "$APP_PACKAGE" >/dev/null 2>&1
        run_adb "$kind" -s "$serial" install "$apk" >/dev/null || fail "installing on $serial failed"
        attach "$kind" "$serial" || status=1
    done < <(handsets)
    [ "$any" = 1 ] || fail "no phone on $PHONES_ADB and no emulator"
    return "$status"
}

# ---- commands ----

healthy() { curl -sf "$API/health" >/dev/null 2>&1; }

cmd_up() {
    need
    mkdir -p "$LOGS"
    if alive api && alive sweeper && alive standin && healthy; then
        note "already up"
        cmd_status
        return 0
    fi
    stop_services
    template_current || build_template
    clone_world
    start_standin
    start_service "$LIVE_DB"
    start_forwarder
    start_ticker
    cmd_status
}

cmd_reset() {
    need
    mkdir -p "$LOGS"
    template_current || { note "the seed is out of date"; build_template; }
    stop_services
    clone_world
    start_standin
    start_service "$LIVE_DB"
    start_forwarder
    start_ticker
    note "reset to the seed"
}

cmd_down() {
    stop ticker
    stop forwarder
    stop_services
    stop standin
    if [ -n "${DATABASE_URL:-}" ]; then
        drop_database "$LIVE_DB"
        drop_database "$SEED_DB"
        admin_sql "drop role if exists $RUNTIME_ROLE" >/dev/null 2>&1
    fi
    rm -rf "$LAB"
    note "down"
}

cmd_status() {
    local name
    for name in standin api sweeper forwarder ticker; do
        if alive "$name"; then printf '  %-10s up (pid %s)\n' "$name" "$(cat "$(pidfile "$name")")"
        elif [ "$name" = ticker ] && [ "$TICK" = manual ]; then printf '  %-10s manual\n' "$name"
        else printf '  %-10s down\n' "$name"; fi
    done
    printf '  api        %s (forwarded on %s:%s)\n' "$API" "$BIND" "$PORT"
    printf '  stand-in   %s\n' "$STANDIN"
    printf '  database   %s\n' "$(db_url "$LIVE_DB")"
    [ -s "$LAB/operator-token" ] && printf '  operator   %s\n' "$LAB/operator-token"
    printf '  logs       %s\n' "$LOGS"
    healthy && printf '  health     %s\n' "$(curl -sf "$API/health")" || printf '  health     not answering\n'
    local kind serial
    while read -r kind serial; do
        [ -n "$kind" ] || continue
        if reaches_api "$kind" "$serial"; then printf '  %-10s %s reaches the API\n' "$kind" "$serial"
        else printf '  %-10s %s does not reach the API\n' "$kind" "$serial"; fi
    done < <(handsets)
    return 0
}

case "${1:-}" in
up) cmd_up ;;
down) cmd_down ;;
reset) cmd_reset ;;
status) cmd_status ;;
devices) cmd_devices ;;
sweep) sweep_once ;;
tick)
    while :; do
        sweep_once >/dev/null 2>&1
        sleep "$TICK"
    done
    ;;
*)
    sed -n '3,12p' "$0" >&2
    exit 2
    ;;
esac
