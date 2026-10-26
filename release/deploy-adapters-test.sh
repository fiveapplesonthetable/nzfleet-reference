#!/bin/bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat > "$work/bin/gcloud" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "$GCLOUD_ARGS"
STUB
chmod +x "$work/bin/gcloud"
GCLOUD_ARGS="$work/gcloud-args" PATH="$work/bin:$PATH" \
    "$root/deploy/gcp-run.sh" service nzela-site europe-west1 describe \
    --format='value(status.url)'
expected="$work/gcloud-expected"
printf '%s\n' run services describe nzela-site --region europe-west1 \
    --format='value(status.url)' > "$expected"
diff -u "$expected" "$work/gcloud-args"
echo 'the Cloud Run adapter passes resource, region, operation, and flags unchanged'

cat > "$work/bin/docker" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" >> "$DOCKER_ARGS"
STUB
cat > "$work/bin/curl" <<'STUB'
#!/bin/bash
exit 0
STUB
chmod +x "$work/bin/docker" "$work/bin/curl"
DOCKER_ARGS="$work/docker-args" PATH="$work/bin:$PATH" \
    "$root/deploy/compose.sh"
printf '%s\n' \
    compose --project-directory "$root/deploy" -f "$root/deploy/compose.prod.yaml" pull nzela worker migrate \
    compose --project-directory "$root/deploy" -f "$root/deploy/compose.prod.yaml" up -d postgres \
    compose --project-directory "$root/deploy" -f "$root/deploy/compose.prod.yaml" run --rm migrate \
    compose --project-directory "$root/deploy" -f "$root/deploy/compose.prod.yaml" up -d --no-deps nzela worker \
    > "$expected"
diff -u "$expected" "$work/docker-args"
echo 'the Compose adapter runs pull, database, migration, and application commands in order'

for adapter in docker binary; do
    grep -q 'nzela-sanctions-refresh.timer' "$root/deploy/$adapter.sh" \
        && grep -q 'enable --now .*nzela-sanctions-refresh.timer' "$root/deploy/$adapter.sh" \
        || { echo "  $adapter deployment does not install the sanctions timer" >&2; exit 1; }
done
cat > "$work/bin/flyctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$FLY_ARGS"
if [[ $1 == machine && $2 == list ]]; then
    printf '[{"id":"old1","name":"adapter-test-sanctions-refresh-20200101000000"},{"id":"web1","name":"adapter-test-web"}]\n'
fi
STUB
chmod +x "$work/bin/flyctl"
cat > "$work/bin/date" <<'STUB'
#!/bin/bash
echo 20260101000000
STUB
chmod +x "$work/bin/date"
FLY_ARGS="$work/fly-args" PATH="$work/bin:$PATH" \
    FLY_APP=adapter-test FLY_REGION=cdg DATABASE_URL=postgres://example \
    GOOGLE_CLIENT_ID=example SANCTIONS_MAX_AGE=48h \
    BLUEFIN_ENVIRONMENT=sandbox BLUEFIN_API_KEY=example BLUEFIN_API_SECRET=example \
    MAX_TRANSFER_CDF_MINOR=1 MAX_TRANSFER_USD_MINOR=1 \
    FLOAT_FEE_CEILING_XOF_MINOR=1 FLOAT_FEE_CEILING_XAF_MINOR=1 \
    FLOAT_FEE_CEILING_BIF_MINOR=1 FLOAT_FEE_CEILING_RWF_MINOR=1 \
    FLOAT_FEE_CEILING_SSP_MINOR=1 FLOAT_FEE_CEILING_TZS_MINOR=1 \
    FLOAT_FEE_CEILING_UGX_MINOR=1 FLOAT_FEE_CEILING_ZMW_MINOR=1 \
    "$root/deploy/cloud.sh" flyio >/dev/null
here="$root/deploy"
printf '%s\n' \
    "deploy $root --app adapter-test --dockerfile $here/Dockerfile --config $here/fly.toml" \
    "machine list --app adapter-test --json" \
    "machine run . sanctions refresh --app adapter-test --name adapter-test-sanctions-refresh-20260101000000 --region cdg --schedule daily --restart on-fail --detach --dockerfile $here/Dockerfile --config $here/fly.toml" \
    "machine destroy old1 --app adapter-test --force" \
    > "$work/fly-expected"
grep -v "^secrets set " "$work/fly-args" > "$work/fly-actual"
diff -u "$work/fly-expected" "$work/fly-actual"
echo 'Compose, Docker, binary, and Fly each schedule the work they own'
