#!/bin/bash
# What db-export.sh promises, against a throwaway Postgres named by DATABASE_URL.
# A database is made for the run, exported, restored into a second one and
# counted. gs:// is held to a stand-in for the Cloud Storage endpoint and ssh://
# to one that runs the remote command here.

set -euo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
[ -n "${DATABASE_URL:-}" ] || { echo "db-export-test: DATABASE_URL names the Postgres to use." >&2; exit 3; }

work="$(mktemp -d)"
src="exp_src_$$"
dst="exp_dst_$$"
server="${DATABASE_URL%/*}"
failed=""
mkdir -p "$work/bin" "$work/out" "$work/tmp"
export TMPDIR="$work/tmp"

cleanup() {
    [ -z "${server_pid:-}" ] || kill "$server_pid" 2>/dev/null || true
    psql -X -q "$DATABASE_URL" -c "drop database if exists $src" -c "drop database if exists $dst" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

fail() { failed="$failed $1"; shift; echo "  $*" >&2; }

# A client of the server's own major version is used as installed. Without one,
# the matching server image supplies it, so a host's client never changes the test.
server_version="$(psql -X -q -At "$DATABASE_URL" -c "select current_setting('server_version_num')")"
server_major="$((server_version / 10000))"
for tool in pg_dump pg_restore; do
    native="$(command -v "$tool" || true)"
    if [ -n "$native" ] && [ "$("$native" --version | sed -nE 's/^.*PostgreSQL\) ([0-9]+).*/\1/p')" = "$server_major" ]; then
        continue
    fi
    printf '#!/bin/bash\nexec docker run --rm -i --network host -u "$(id -u):$(id -g)" -v "%s:%s" postgres:%s %s "$@"\n' \
        "$work" "$work" "$server_major" "$tool" > "$work/bin/$tool"
    chmod +x "$work/bin/$tool"
done

cat > "$work/bin/ssh" <<'STUB'
#!/bin/bash
while [ "$#" -gt 2 ] && [ "${1:0:1}" = - ]; do
    case "$1" in -o|-i|-p) shift 2 ;; *) shift ;; esac
done
shift
bash -c "$*"
STUB
cat > "$work/bin/gcloud" <<'STUB'
#!/bin/bash
echo test-token
STUB
chmod +x "$work/bin/ssh" "$work/bin/gcloud"
export PATH="$work/bin:$PATH"

psql -X -q -v ON_ERROR_STOP=1 "$DATABASE_URL" -c "create database $src template template0" -c "create database $dst template template0" >/dev/null \
    || { echo "db-export-test: could not create the scratch databases." >&2; exit 3; }
psql -X -q -v ON_ERROR_STOP=1 "$server/$src" >/dev/null <<'SQL'
create table senders (id bigint primary key, name text not null);
create table entries (id bigint primary key, sender bigint references senders, amount_minor bigint not null);
insert into senders select n, 'sender ' || n from generate_series(1, 40) n;
insert into entries select n, 1 + n % 40, n * 100 from generate_series(1, 500) n;
SQL

run() { DATABASE_URL="$server/$src" "$here/db-export.sh" "$@"; }
counts() { psql -X -q -At "$server/$1" -c "select (select count(*) from senders) || ' ' || (select count(*) from entries)"; }

echo "a file:// export restores to the same rows"
run "file://$work/out/nzela" >"$work/export.out" 2>&1 || fail file "exit $?: $(cat "$work/export.out")"
dump="$(ls "$work/out/nzela" 2>/dev/null | grep -E '^[0-9]{8}T[0-9]{6}\.dump$' || true)"
[ -n "$dump" ] || fail named "no <UTC stamp>.dump in the destination: $(ls -A "$work/out/nzela" 2>&1)"
[ "$(ls -A "$work/out/nzela" | wc -l)" -eq 1 ] || fail partial "something besides the dump was left behind"
pg_restore --no-owner --dbname "$server/$dst" "$work/out/nzela/$dump" 2>"$work/restore.err" \
    || fail restore "pg_restore: $(head -n 5 "$work/restore.err")"
[ "$(counts "$src")" = "$(counts "$dst")" ] && [ "$(counts "$dst")" = "40 500" ] \
    || fail counts "source $(counts "$src"), restored $(counts "$dst"), expected 40 500"

echo "an ssh:// export lands in the directory it names"
run "ssh://nzbackup@far.example:2222$work/out/far" >"$work/ssh.out" 2>&1 || fail ssh "exit $?: $(cat "$work/ssh.out")"
ls "$work/out/far" 2>/dev/null | grep -qE '^[0-9]{8}T[0-9]{6}\.dump$' || fail ssh-named "no dump in $work/out/far"

echo "a gs:// export is sent to the bucket and checked against the reply"
cat > "$work/gcs.py" <<'PY'
import base64, hashlib, http.server, json, os, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        if self.headers.get("x-goog-hash") != "md5=" + md5:
            self.send_response(400); self.end_headers(); return
        size = len(body) - int(os.environ.get("DROP_BYTES", "0"))
        with open(os.environ["LOG"], "a") as log:
            log.write(self.headers["Authorization"] + " " + self.path + "\n")
        reply = json.dumps({"size": str(size), "md5Hash": md5}, indent=2).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(reply))); self.end_headers()
        self.wfile.write(reply)
    def log_message(self, *args): pass

server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
PY
mkfifo "$work/port"
LOG="$work/gcs.log" python3 -I "$work/gcs.py" > "$work/port" &
server_pid=$!
read -r port < "$work/port"
export NZELA_GCS_ENDPOINT="http://127.0.0.1:$port"
run "gs://backups/nzela/" >"$work/gs.out" 2>&1 || fail gs "exit $?: $(cat "$work/gs.out")"
grep -qE '^Bearer test-token /upload/storage/v1/b/backups/o\?uploadType=media&name=nzela%2F[0-9]{8}T[0-9]{6}\.dump$' "$work/gcs.log" \
    || fail gs-object "the upload went to: $(cat "$work/gcs.log" 2>&1)"

echo "a bucket that stored fewer bytes than were sent is a failure"
kill "$server_pid"; wait "$server_pid" 2>/dev/null || true
DROP_BYTES=1 LOG="$work/gcs.log" python3 -I "$work/gcs.py" > "$work/port" &
server_pid=$!
read -r port < "$work/port"
if NZELA_GCS_ENDPOINT="http://127.0.0.1:$port" run "gs://backups/nzela/" >"$work/short.out" 2>&1; then
    fail short "expected exit 1: $(cat "$work/short.out")"
else
    status=$?
    [ "$status" -eq 1 ] || fail short "expected exit 1, got $status: $(cat "$work/short.out")"
fi

echo "s3:// says it is not implemented"
if run "s3://backups/nzela/" >"$work/s3.out" 2>&1; then
    fail s3 "expected exit 3: $(cat "$work/s3.out")"
else
    status=$?
    [ "$status" -eq 3 ] && grep -q "not implemented" "$work/s3.out" || fail s3 "$(cat "$work/s3.out")"
fi

echo "a database that cannot be reached writes nothing"
if DATABASE_URL="postgres://nobody:x@127.0.0.1:1/none" "$here/db-export.sh" "file://$work/out/none" >"$work/none.out" 2>&1; then
    fail unreachable "expected exit 3: $(cat "$work/none.out")"
else
    status=$?
    [ "$status" -eq 3 ] && [ -z "$(ls -A "$work/out/none" 2>/dev/null)" ] || fail unreachable "$(cat "$work/none.out")"
fi

if [ -n "$failed" ]; then
    echo "db-export-test: FAILED:$failed" >&2
    exit 1
fi
echo "db-export-test: passed"
