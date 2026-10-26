#!/usr/bin/env bash
# The image a pull request run built is pushed for the commit that merged it, and only when the
# archive holds exactly the image that run recorded.
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$CALLS"
case "$1 ${2:-}" in
  'load --quiet') test "$3 $4" = "-i $TMP/image.tar"; touch "$TMP/loaded" ;;
  'image inspect')
    if [ "${3:-}" = --format ] && [ "${4:-}" = '{{.Id}}' ]; then test -f "$TMP/loaded"; printf '%s\n' "$LOADED"; exit; fi
    test -f "$TMP/tagged"; printf 'registry.example/nzela@%s\n' "$RESOLVES" ;;
  'tag '*) touch "$TMP/tagged" ;;
  'manifest inspect') test -f "$TMP/pushed" || { echo 'no such manifest' >&2; exit 1; } ;;
  'push --quiet') touch "$TMP/pushed" ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 2 ;;
esac
DOCKER
chmod +x "$tmp/bin/docker"
: > "$tmp/image.tar"
export PATH="$tmp/bin:$PATH" TMP="$tmp" CALLS="$tmp/calls"
id="sha256:$(printf '%064d' 0 | tr 0 a)"
digest="sha256:$(printf '%064d' 0 | tr 0 e)"
export LOADED=$id RESOLVES=$digest

got=$("$root/tools/nzfleet/release/reuse-image.sh" registry.example "$id" abc123 "$tmp/image.tar")
[ "$got" = "registry.example/nzela@$digest" ]
! grep -q '^build' "$CALLS"

rm -f "$tmp/loaded" "$tmp/tagged" "$tmp/pushed"
if LOADED="sha256:$(printf '%064d' 0 | tr 0 b)" "$root/tools/nzfleet/release/reuse-image.sh" registry.example "$id" abc123 "$tmp/image.tar" 2>/dev/null; then exit 1; fi
! test -f "$tmp/pushed"

if "$root/tools/nzfleet/release/reuse-image.sh" registry.example latest abc123 "$tmp/image.tar" 2>/dev/null; then exit 1; fi
if "$root/tools/nzfleet/release/reuse-image.sh" registry.example "$id" abc123 "$tmp/missing.tar" 2>/dev/null; then exit 1; fi
printf '%s\n' 'an archived image is tagged for the commit and pushed without a build; a different image, a malformed id or a missing archive is refused'
