#!/bin/bash
# Delete Actions caches not used, and artifacts not created, for DAYS days.
#
#   REPO=nzela-org/nzela tools/nzfleet/release/actions-tidy.sh [days]    # default 3
#   DRY_RUN=1 tools/nzfleet/release/actions-tidy.sh                      # list, delete nothing
#
# Both pile up toward the 10 GB cap: every lockfile change leaves a cache nobody
# reads again, and artifacts outlive the run that wanted them. The scheduled
# workflow runs this; an owner can run it by hand with the same effect.

set -euo pipefail

repo="${REPO:-${GITHUB_REPOSITORY:?name the repository in REPO}}"
days="${1:-3}"
[[ "$days" =~ ^[1-9][0-9]*$ ]] || { echo "actions-tidy: days is a whole number." >&2; exit 2; }
cutoff="$(date -u -d "$days days ago" +%Y-%m-%dT%H:%M:%SZ)"
failed=0

# sweep KIND COLLECTION DATE_FIELD: delete each item whose date is before the cutoff.
sweep() {
    local kind="$1" collection="$2" field="$3" id n=0 items
    items="$(gh api --paginate "repos/$repo/actions/$kind?per_page=100" \
        --jq ".$collection[] | select(.$field < \"$cutoff\") | .id")"
    while read -r id; do
        [ -n "$id" ] || continue
        if [ -n "${DRY_RUN:-}" ]; then
            echo "would delete $kind $id"
        elif gh api -X DELETE "repos/$repo/actions/$kind/$id" >/dev/null; then
            n=$((n + 1))
        else
            echo "actions-tidy: could not delete $kind $id" >&2
            failed=1
        fi
    done <<< "$items"
    echo "actions-tidy: $n $kind deleted, older than $cutoff"
}

sweep caches actions_caches last_accessed_at
sweep artifacts artifacts created_at
exit "$failed"
