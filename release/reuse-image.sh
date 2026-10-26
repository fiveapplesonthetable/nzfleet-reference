#!/usr/bin/env bash
# Pushes the release image a pull request run built, from its archive, as this commit's image and
# prints the digest reference deploy will accept. Nothing is built. The archive is used only if it
# holds the image id that run recorded.
#
#   tools/nzfleet/release/reuse-image.sh REGISTRY IMAGE_ID SHA ARCHIVE
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 REGISTRY IMAGE_ID SHA ARCHIVE" >&2
  exit 2
fi
registry=$1 id=$2 sha=$3 archive=$4

case "$id" in
  sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "not an image id: '$id'" >&2; exit 1 ;;
esac
[ -f "$archive" ] || { echo "no image archive at $archive" >&2; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
docker load --quiet -i "$archive" >/dev/null
held=$(docker image inspect --format '{{.Id}}' "$id")
if [ "$held" != "$id" ]; then
  echo "the archive holds $held, not the recorded $id" >&2
  exit 1
fi
target="$registry/nzela:$sha"
docker tag "$id" "$target"
"$here/push-image.sh" "$target"
