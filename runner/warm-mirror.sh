#!/usr/bin/env bash
# Keeps a bare mirror of this repository's history on the runner, so a job
# that needs the full history borrows it instead of downloading it again.
#   tools/nzfleet/runner/warm-mirror.sh keep MIRROR   copy this checkout's history into MIRROR
# A job borrows with one line before its checkout: MIRROR/objects in
# .git/objects/info/alternates. The checkout still fetches and verifies the
# commit it was asked for, so a stale mirror costs time and never correctness.
set -euo pipefail

if [ "$#" -ne 2 ] || [ "$1" != keep ]; then
  echo "usage: $0 keep MIRROR" >&2
  exit 2
fi

mirror=$2
[ -d "$mirror/objects" ] || git init --bare -q "$mirror"
git --git-dir="$mirror" fetch -q --no-tags "$PWD" \
  '+refs/remotes/origin/*:refs/heads/*' '+HEAD:refs/checked-out'
