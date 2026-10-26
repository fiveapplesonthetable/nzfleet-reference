#!/bin/bash
# tools/nzfleet/release/secret-version.sh takes the value on standard input and puts it in no
# argument, refuses an empty value, and refuses a secret Terraform does not own.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/gcloud" <<'F'
#!/bin/bash
printf '%s\n' "$*" > "$GCLOUD_ARGS"
cat > "$GCLOUD_STDIN"
echo 7
F
chmod +x "$work/gcloud"
export PATH="$work:$PATH" GCLOUD_ARGS="$work/args" GCLOUD_STDIN="$work/stdin"
bad=0
expect() { printf '%s\n' "$1" >&2; bad=1; }

out="$(printf 'sk_live_abc' | "$root/tools/nzfleet/release/secret-version.sh" nzela-bluefin-api-key 2>/dev/null)"
[ "$out" = 7 ] || expect "the version number gcloud printed was not passed on: $out"
[ "$(cat "$work/stdin")" = sk_live_abc ] || expect "the value did not reach gcloud on standard input"
grep -q sk_live_abc "$work/args" && expect "the value was in gcloud's arguments"

printf '' | "$root/tools/nzfleet/release/secret-version.sh" nzela-bluefin-api-key >/dev/null 2>&1 &&
    expect "an empty value added a version"
printf 'x' | "$root/tools/nzfleet/release/secret-version.sh" nzela-database-url >/dev/null 2>&1 &&
    expect "a secret holding a generated credential accepted a hand-made version"
printf 'x' | "$root/tools/nzfleet/release/secret-version.sh" nzela-nothing >/dev/null 2>&1 &&
    expect "a secret Terraform does not own was accepted"

[ "$bad" -eq 0 ] && echo "secret versions are added from standard input"
exit "$bad"
