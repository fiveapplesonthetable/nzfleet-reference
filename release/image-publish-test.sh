#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
case "$1 ${2:-} ${3:-}" in
  'manifest inspect '*) test -f "$REMOTE_IMAGE" || { echo 'no such manifest' >&2; exit 1; } ;;
  'pull --quiet '*) test -f "$REMOTE_IMAGE"; touch "$LOCAL_IMAGE"; printf 'pull\n' >> "$PULL_CALLS" ;;
  'image inspect '*)
    test -f "$LOCAL_IMAGE"
    case "$*" in
      *RepoDigests*) test -f "$REMOTE_IMAGE" && printf '%s\n' 'registry.example/other@sha256:wrong' 'registry.example/nzela/api@sha256:verified' ;;
    esac
    ;;
  'push --quiet '*) touch "$REMOTE_IMAGE"; printf 'push\n' >> "$PUSH_CALLS" ;;
  'build '*)
    printf '%s\n' "$*" >> "$BUILD_CALLS"
    touch "$LOCAL_IMAGE"
    ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 2 ;;
esac
DOCKER
chmod +x "$tmp/bin/docker"
export PATH="$tmp/bin:$PATH"
export REMOTE_IMAGE="$tmp/remote" LOCAL_IMAGE="$tmp/local" BUILD_CALLS="$tmp/build-calls" PUSH_CALLS="$tmp/push-calls" PULL_CALLS="$tmp/pull-calls"
image='registry.example/nzela/api:commit'

built=$("$root/tools/nzfleet/release/build-images.sh" "$image" deploy/Dockerfile . commit)
[ -z "$built" ]
grep -q -- '--build-arg COMMIT=commit' "$BUILD_CALLS"

published=$("$root/tools/nzfleet/release/push-image.sh" "$image" 2>"$tmp/stderr")
[ ! -s "$tmp/stderr" ]
[ "$published" = 'registry.example/nzela/api@sha256:verified' ]
[ -f "$REMOTE_IMAGE" ]
[ "$(wc -l < "$PUSH_CALLS")" -eq 1 ]

rm "$LOCAL_IMAGE"
existing=$("$root/tools/nzfleet/release/push-image.sh" "$image")
[ "$existing" = 'registry.example/nzela/api@sha256:verified' ]
[ "$(wc -l < "$PUSH_CALLS")" -eq 1 ]
[ "$(wc -l < "$PULL_CALLS")" -eq 1 ]
rm "$LOCAL_IMAGE" "$REMOTE_IMAGE"
if "$root/tools/nzfleet/release/push-image.sh" "$image" >/dev/null 2>"$tmp/stderr"; then exit 1; fi
grep -q 'no such manifest' "$tmp/stderr"
printf '%s\n' 'missing image pushed; an existing image pulled, not pushed again; both resolved by digest'
