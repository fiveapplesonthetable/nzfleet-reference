#!/usr/bin/env bash
# A directory left to root, with a file in it, is writable by a container running as the caller after the
# hand-over, and the caller's own directory stays writable. The probe runs in a container because on a rootless
# daemon root inside is the caller outside, so only the container's own view tells the two apart.
set -euo pipefail
cd "$(dirname "$0")/../../.."
image=$(sed -n 's/^FROM \(rust:[^ ]*\) AS build$/\1/p' deploy/Dockerfile)
work="$(mktemp -d "${TMPDIR:-/tmp}/hand-over.XXXXXX")"
trap 'docker run --rm --user 0:0 -v "$work:/w" "$image" rm -rf /w/out /w/mine >/dev/null 2>&1; rm -rf "$work"' EXIT
mkdir "$work/out" "$work/mine"

writable_as_caller() {
    docker run --rm --user "$(id -u):$(id -g)" -v "$1:/d" "$image" touch /d/probe 2>/dev/null
}

docker run --rm --user 0:0 -v "$work/out:/o" "$image" sh -c 'touch /o/nzela && chown -R 0:0 /o && chmod 755 /o /o/nzela'
writable_as_caller "$work/out" && { echo "the fixture directory is writable before the hand-over" >&2; exit 1; }

tools/nzfleet/release/hand-over.sh "$image" "$work/out" "$work/mine"
writable_as_caller "$work/out" || { echo "the output directory is still not writable" >&2; exit 1; }
writable_as_caller "$work/mine" || { echo "the caller's own directory stopped being writable" >&2; exit 1; }
echo "a directory left to root is writable by the caller after the hand-over"
