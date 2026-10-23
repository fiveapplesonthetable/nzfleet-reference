#!/bin/bash
set -euo pipefail

source=$1 destination=$2 origin=$3
git clone -q --depth=1 --no-local --no-tags "$source" "$destination"
git -C "$destination" fetch -q --depth=1 --no-tags "$source" HEAD
git -C "$destination" checkout -q --detach FETCH_HEAD
git -C "$destination" remote set-url origin "$origin"
git -C "$destination" config remote.origin.fetch '+refs/heads/main:refs/remotes/origin/main'
while IFS= read -r ref; do
    git -C "$destination" update-ref -d "$ref"
done < <(git -C "$destination" for-each-ref --format='%(refname)' refs/heads refs/remotes)
