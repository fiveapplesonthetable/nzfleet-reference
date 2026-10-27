#!/bin/bash
# Delete Actions caches not used, and artifacts not created, for DAYS days, and
# every artifact past the newest KEEP of its name once its run has had two hours.
#
#   REPO=nzela-org/nzela tools/nzfleet/release/actions-tidy.sh [days]    # default 3
#   DRY_RUN=1 tools/nzfleet/release/actions-tidy.sh                      # list, delete nothing
#
# Both pile up toward the storage cap: every lockfile change leaves a cache nobody
# reads again, and each CI run leaves a 330 MB test archive that a one-day
# retention still multiplies by every run of the day. The scheduled workflow
# runs this; an owner can run it by hand with the same effect.

set -euo pipefail

repo="${REPO:-${GITHUB_REPOSITORY:?name the repository in REPO}}"
days="${1:-3}"
[[ "$days" =~ ^[1-9][0-9]*$ ]] || { echo "actions-tidy: days is a whole number." >&2; exit 2; }
keep="${KEEP:-3}"
cutoff="$(date -u -d "$days days ago" +%Y-%m-%dT%H:%M:%SZ)"
settled="$(date -u -d "2 hours ago" +%Y-%m-%dT%H:%M:%SZ)"
# A later run reads these from the pull request run that made them, so only age removes them.
read_later='^(release-image|ci-proof|verified-image-digest)$'
failed=0

# remove KIND IDS: delete each id, counting what went.
remove() {
    local kind="$1" items="$2" id n=0
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
    echo "actions-tidy: $n $kind deleted"
}

caches="$(gh api --paginate "repos/$repo/actions/caches?per_page=100" \
    --jq ".actions_caches[] | select(.last_accessed_at < \"$cutoff\") | .id")"
artifacts="$(gh api --paginate "repos/$repo/actions/artifacts?per_page=100" \
    --jq '.artifacts[] | select(.expired | not) | [.id, .name, .created_at] | @tsv')"
remove caches "$caches"
remove artifacts "$(sort -t $'\t' -k2,2 -k3,3r <<< "$artifacts" | awk -F '\t' \
    -v keep="$keep" -v cutoff="$cutoff" -v settled="$settled" -v later="$read_later" '
    NF == 3 { rank[$2]++
              if ($3 < cutoff || ($2 !~ later && rank[$2] > keep && $3 < settled)) print $1 }')"
exit "$failed"
