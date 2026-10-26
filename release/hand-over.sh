#!/usr/bin/env bash
# Gives the caller ownership of directories an earlier run left to another user, so a container
# started as the caller can write them. Runs as root inside IMAGE; a directory the caller owns
# throughout is left alone.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 IMAGE DIR..." >&2
  exit 2
fi

image=$1
shift
uid=$(id -u)
gid=$(id -g)
mounts=()
targets=()
for dir in "$@"; do
  mkdir -p "$dir"
  mounts+=(-v "$(cd "$dir" && pwd):/handover/${#targets[@]}")
  targets+=("/handover/${#targets[@]}")
done
docker run --rm --user 0:0 "${mounts[@]}" "$image" \
  sh -c "find ${targets[*]} ! -user $uid -print -quit | grep -q . && chown -R $uid:$gid ${targets[*]} || true"
