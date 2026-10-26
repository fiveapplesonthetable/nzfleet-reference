#!/usr/bin/env bash
# Builds the release binary the image ships, in the toolchain image the
# Dockerfile builds in, so it links the glibc the runtime base carries.
# The cargo cache lives in CACHE and survives between runs.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 OUT_DIR CACHE_DIR" >&2
  exit 2
fi

root=$(cd "$(dirname "$0")/../../.." && pwd)
out=$1
cache=$2
toolchain=$(sed -n 's/^FROM \(rust:[^ ]*\) AS build$/\1/p' "$root/deploy/Dockerfile")
[ -n "$toolchain" ] || { echo "no build stage in deploy/Dockerfile" >&2; exit 1; }

mkdir -p "$out" "$cache"
uid=$(id -u)
gid=$(id -g)
# The cache and the output directory outlive the user that first filled them.
"$root/tools/nzfleet/release/hand-over.sh" "$toolchain" "$cache" "$out"
docker run --rm \
  --user "$uid:$gid" \
  -v "$root:/src:ro" -v "$cache:/cache" -v "$out:/out" \
  -w /src \
  -e HOME=/cache/home -e CARGO_HOME=/cache/cargo -e CARGO_TARGET_DIR=/cache/target \
  -e SQLX_OFFLINE=true \
  "$toolchain" \
  sh -c 'cargo build --release --locked -p nzela && cp /cache/target/release/nzela /out/nzela'
