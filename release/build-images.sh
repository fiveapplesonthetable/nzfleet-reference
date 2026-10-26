#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 IMAGE DOCKERFILE CONTEXT COMMIT" >&2
  exit 2
fi

image=$1
dockerfile=$2
context=$3
commit=$4

docker build \
  --file "$dockerfile" \
  --build-arg "COMMIT=$commit" \
  --tag "$image" \
  "$context"
