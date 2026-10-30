#!/bin/bash
# merge-check.sh <run-dir> <port> <areas> <pr:sha>...
#
# Merges each PR head onto upstream/main in order in a scratch worktree, skipping
# any that conflict, then runs scripts/checks.sh once over the comma-separated
# areas, each the way ci runs it: in the job's image where ci uses one, on the
# host where it does not (nzfleet/areas.py). Writes <run-dir>/result.json and
# <run-dir>/log. Everything it creates is named after the run directory and
# removed on exit.
set -u
run=$1 port=$2 areas=$3
shift 3
repo=${NZFLEET_REPO:?}
fleet=$(cd "$(dirname "$0")/.." && pwd)
name=$(basename "$run")
wt=${NZFLEET_WORK:?}/nzf-$name
check_wt=$run/checkout
pg=nzf-$name-pg
target=${NZFLEET_TARGET:-/var/tmp/nzt}/nzf-$name
home=${NZFLEET_HOME:-$HOME/.nzfleet}
warm=${NZFLEET_WARM-$home/warm}
mkdir -p "$run"
exec >"$run/log" 2>&1

# A warm slot is a worktree and a build directory that outlive the batch, so the next one compiles, resolves and
# installs only what changed. One batch holds a slot at a time; with none free for a while the batch builds cold,
# as it always did, rather than queueing behind another.
slot=""
claim_slot() {
    [ -n "$warm" ] || return 1
    local n deadline=$((SECONDS + ${NZFLEET_WARM_WAIT:-90}))
    mkdir -p "$warm" || return 1
    while :; do
        for n in $(seq "${NZFLEET_WARM_SLOTS:-3}"); do
            exec {slot_fd}>"$warm/slot-$n.lock" || return 1
            if flock -n "$slot_fd"; then
                slot=$warm/slot-$n
                return 0
            fi
            exec {slot_fd}>&-
        done
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 3
    done
}

cleanup() {
    # What the areas redrew outlives the worktree, for the branches the batch then commits it to.
    [ -d "$check_wt/shot-report" ] && cp -a "$check_wt/shot-report" "$run/shots"
    held=$(awk -F'\t' '$3 == "golden" { print "  " $2 }' "$check_wt"/shot-report/*/changed.txt 2>/dev/null)
    [ -n "$held" ] && printf 'golden screenshots moved:\n%s\nIf the change meant to move them, run in the branch: nzfleet shots accept %s\n' "$held" "$name"
    rm -rf -- "$check_wt"
    git -C "$repo" for-each-ref --format='%(refname)' "refs/nzf/check/$name/" | xargs -r -n1 git -C "$repo" update-ref -d
    docker rm -f "$pg" >/dev/null 2>&1
    if [ -z "$slot" ]; then
        git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1
        rm -rf "$target"
    fi
}
trap cleanup EXIT

result() {
    python3 - "$run/result.json" "$@" <<'PY'
import json, sys
path, rc, merged, skipped, line = sys.argv[1:6]
tree = sys.argv[6] if len(sys.argv) > 6 else ""
skips = {}
try:
    for row in open(path.replace("result.json", "skips.tsv")):
        pr, why, files = (row.rstrip("\n").split("\t") + ["", ""])[:3]
        skips[pr] = {"why": why, "files": files.split()}
except OSError:
    pass
try:
    stats = json.load(open(path.replace("result.json", "stats.json")))
except (OSError, ValueError):
    stats = {}
json.dump({"rc": int(rc), "merged": merged.split(), "skipped": skipped.split(),
           "skips": skips, "line": line, "tree": tree, "stats": stats}, open(path, "w"))
PY
}

git -C "$repo" fetch -q upstream main || { result 90 "" "" "fetch of main failed"; exit 0; }
if claim_slot; then
    wt=$slot/tree target=$slot/target
    mkdir -p "$target"
    echo "warm: $slot"
    if [ -e "$wt/.git" ]; then
        # Ignored files stay: they are the build. Whatever the last batch merged or regenerated does not.
        git -C "$wt" reset -q --hard && git -C "$wt" clean -fdq && git -C "$wt" checkout -q --detach upstream/main \
            || { rm -rf "$wt"; git -C "$repo" worktree prune; }
    fi
    [ -e "$wt/.git" ] || git -C "$repo" worktree add -q --detach -f "$wt" upstream/main || { result 90 "" "" "worktree failed"; exit 0; }
else
    slot=""
    echo "warm: no slot, building cold"
    git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1
    git -C "$repo" worktree add -q --detach "$wt" upstream/main || { result 90 "" "" "worktree failed"; exit 0; }
fi
cd "$wt" || exit 0

# Two PRs that each redrew the screenshots conflict there and nowhere else. The
# check does not compare screenshots byte for byte, so the train's copy stands in;
# the batch's refresh step draws them again on the merged tree.
generated='^frontend/admin/(screenshots/|docs/comparison\.html$)|^frontend/android/docs/screenshots/'
settle_generated() {
    local files
    files=$(git diff --name-only --diff-filter=U)
    [ -n "$files" ] || return 1
    printf '%s\n' "$files" | grep -qvE "$generated" && return 1
    while IFS= read -r f; do
        if git cat-file -e "HEAD:$f" 2>/dev/null; then
            git checkout --ours -- "$f" && git add -- "$f"
        else
            git rm -q -- "$f"
        fi
    done <<<"$files"
    git -c user.email=nzfleet@local -c user.name=nzfleet commit -q --no-edit
}

merged="" skipped=""
skip() { # PR, reason, conflicted files
    skipped="$skipped $1"
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$run/skips.tsv"
}
: >"$run/skips.tsv"
for item in "$@"; do
    pr=${item%%:*} sha=${item#*:}
    # A ref of this run's own: FETCH_HEAD is shared with every other fetch in the repository.
    ref=refs/nzf/check/$name/$pr
    if ! git -C "$repo" fetch -q upstream "+pull/$pr/head:$ref" 2>/dev/null; then
        skip "$pr" "its head could not be fetched" ""
        continue
    fi
    if [ "$(git -C "$repo" rev-parse "$ref")" != "$sha" ]; then
        skip "$pr" "its head moved after the batch was formed" ""
        continue
    fi
    if git -c user.email=nzfleet@local -c user.name=nzfleet merge -q --no-edit "$sha"; then
        merged="$merged $pr"
    elif settle_generated; then
        merged="$merged $pr"
    else
        files=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
        git merge --abort
        skip "$pr" "CONFLICT" "$files"
    fi
done
echo "merged:$merged skipped:$skipped"
if [ -z "$merged" ]; then
    result 93 "" "$skipped" "nothing merged"
    exit 0
fi
# Taken before the areas run, which regenerate files: this is the tree that was merged, the one main will have.
tree=$(git rev-parse 'HEAD^{tree}')
rm -rf -- "$check_wt"
if ! "$fleet/bin/shallow-checkout.sh" "$wt" "$check_wt" "$(git -C "$repo" remote get-url upstream)"; then
    result 90 "$merged" "$skipped" "shallow checkout failed"
    exit 0
fi
cd "$check_wt" || exit 0
# ci fetches the base in a step of its own before checks.sh; the areas here run checks.sh alone.
main_sha=$(git -C "$repo" rev-parse upstream/main)
git fetch -q --depth=1 --no-tags "$wt" "$main_sha" || { result 90 "$merged" "$skipped" "fetch of the base commit failed"; exit 0; }
export GITHUB_SHA
GITHUB_SHA=$(git rev-parse HEAD)

docker_budget=$(PYTHONPATH="$wt/tools/nzfleet" python3 -m nzfleet.docker_budget --cpus 2 --managed) || {
    result 90 "$merged" "$skipped" "Docker budget cannot be enforced"
    exit 0
}
read -r -a docker_flags <<<"$docker_budget"
export NZELA_MANAGED_DOCKER=1
docker rm -f "$pg" >/dev/null 2>&1
docker run -d "${docker_flags[@]}" --name "$pg" -p "127.0.0.1:$port:5432" --tmpfs /var/lib/postgresql/data:rw,size=4g \
    -e POSTGRES_PASSWORD=nzela postgres:16 -c fsync=off -c synchronous_commit=off \
    -c full_page_writes=off -c max_connections=300 >/dev/null
# Over TCP, because the image's init server listens on its socket only and answers early.
until docker exec "$pg" psql -h 127.0.0.1 -U postgres -qAtc 'select 1' >/dev/null 2>&1; do
    [ "$(docker inspect -f '{{.State.Running}}' "$pg" 2>/dev/null)" = true ] || { docker logs "$pg" >&2; exit 1; }
    sleep 0.5
done
psql=${NZFLEET_PSQL:-psql}
$psql "postgres://postgres:nzela@127.0.0.1:$port/postgres" -qc "create database nzela" >/dev/null
export DATABASE_URL=postgres://postgres:nzela@127.0.0.1:$port/nzela
for f in $(ls crates/*/migrations/*.sql | sort -t/ -k4); do
    if ! $psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f" >/dev/null; then
        result 91 "$merged" "$skipped" "migration failed: $f"
        exit 0
    fi
done

export CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
# A warm target keeps the workspace's own crates between batches, so only what a pull request touched is
# recompiled; a cold one would only pay for the incremental cache it never reads. Cargo compares a source's
# modification time with its artifact's, which holds because git is the only thing that writes the tree.
if [ -n "$slot" ]; then export CARGO_INCREMENTAL=1; else export CARGO_INCREMENTAL=0; fi
export ANDROID_HOME=${ANDROID_HOME:-} ADB=false
unset NZELA_CONSOLE_WORKERS
if [ -n "$slot" ]; then
    # A new compiler leaves nothing reusable, and a build directory grows with every dependency that ever was.
    toolchain=$(grep -ho 'rust:[0-9][^[:space:]]*' .github/workflows/ci.yaml | sort -u | tr '\n' ' ')
    if [ "$(cat "$target/.toolchain" 2>/dev/null)" != "$toolchain" ]; then
        echo "warm: starting the build directory again (toolchain $toolchain)"
        find "$target" -mindepth 1 -delete
    else
        PYTHONPATH=$fleet python3 -m nzfleet shrink-warm "$target" "${NZFLEET_WARM_MAX_GB:-60}"
    fi
    echo "$toolchain" >"$target/.toolchain"
fi
ln -sfn "$target" target
echo "sdk.dir=$ANDROID_HOME" >frontend/android/local.properties

sccache=${NZFLEET_SCCACHE-$home/sccache} sccache_bin=""
[ -z "$sccache" ] || [ ! -x tools/nzfleet/runner/install-sccache.sh ] || sccache_bin=$(tools/nzfleet/runner/install-sccache.sh "$home/bin")

# The workflow is the merged tree's own, so a PR that changes a job is checked by the job it wrote.
python3 -c 'import os; cpus = sorted(os.sched_getaffinity(0)); print(f"area CPU affinity: {len(cpus)} CPUs ({cpus})")'
PYTHONPATH=$fleet python3 -m nzfleet.areas --workflow .github/workflows/ci.yaml --worktree "$check_wt" \
    --name "nzf-$name" --target "$target" --main "$main_sha" \
    --cache "${NZFLEET_CACHE:-$home/cache}" --slot "${NZFLEET_GRADLE_SLOT:-}" \
    --sccache "$sccache" --sccache-bin "$sccache_bin" --stats "$run/stats.json" ${areas//,/ }
rc=$?
# Only checks.sh's closing line: a test that prints "failed: <name>" must not stand in for it.
line=$(PYTHONPATH=$fleet python3 -m nzfleet summary-line "$run/log" $("$fleet/bin/nzfleet-select" areas all) ${areas//,/ })
result "$rc" "$merged" "$skipped" "$line" "$tree"
