#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 IMAGE" >&2
  exit 2
fi

image=$1

# Plain docker only: the self-hosted runners carry no buildx plugin, and a
# digest read back from the registry is the one Cloud Run will pull.
# "Not pushed yet" is an expected manifest error; its text is shown only when
# the local image is missing too, so a fresh build does not log a failure.
if inspect_error=$(docker manifest inspect "$image" 2>&1 >/dev/null); then
  docker pull --quiet "$image" >/dev/null
else
  if ! docker image inspect "$image" >/dev/null; then
    printf '%s\n' "$inspect_error" >&2
    exit 1
  fi
  docker push --quiet "$image" >/dev/null
fi

repository=${image%:*}
pinned=$(docker image inspect "$image" --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -F "$repository@" | head -n 1 || true)

if [ -z "$pinned" ]; then
  echo "could not resolve digest for $image" >&2
  exit 1
fi

printf '%s\n' "$pinned"
