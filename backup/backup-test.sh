#!/bin/bash
# The backup adapter sends one complete compressed dump to each supported store.

set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/nzela-backup-test.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/store"

cat > "$work/bin/pg_dump" <<'SH'
#!/bin/sh
printf '%s\n' '-- PostgreSQL database dump complete'
SH
cat > "$work/bin/gsutil" <<'SH'
#!/bin/sh
test "$1" = cp && cp "$2" "$GS_CAPTURE"
SH
cat > "$work/bin/aws" <<'SH'
#!/bin/sh
test "$1 $2" = 's3 cp' && cp "$3" "$AWS_CAPTURE"
SH
chmod +x "$work/bin/pg_dump" "$work/bin/gsutil" "$work/bin/aws"

run_backup() {
    PATH="$work/bin:$PATH" DATABASE_URL=postgres://unused \
        ARTIFACT_STORE="$1" TMPDIR="$work" "$root/tools/nzfleet/backup/backup.sh"
}

local_url="$(run_backup "file://$work/store")"
test -s "${local_url#file://}"
gzip -t "${local_url#file://}"
echo "file:// exports a readable compressed dump"

GS_CAPTURE="$work/google.sql.gz"; export GS_CAPTURE
google_url="$(run_backup gs://bucket/exports)"
test -s "$GS_CAPTURE"
gzip -t "$GS_CAPTURE"
case "$google_url" in gs://bucket/exports/nzela/*.sql.gz) ;; *) exit 1 ;; esac
echo "gs:// uploads through gsutil"

AWS_CAPTURE="$work/aws.sql.gz"; export AWS_CAPTURE
s3_url="$(run_backup s3://bucket/exports)"
test -s "$AWS_CAPTURE"
gzip -t "$AWS_CAPTURE"
case "$s3_url" in s3://bucket/exports/nzela/*.sql.gz) ;; *) exit 1 ;; esac
echo "s3:// uploads through aws s3"

if ARTIFACT_STORE=ftp://bucket DATABASE_URL=postgres://unused "$root/tools/nzfleet/backup/backup.sh" >/dev/null 2>&1; then
    echo "backup accepted an unsupported store" >&2
    exit 1
fi
echo "unsupported stores are refused"
