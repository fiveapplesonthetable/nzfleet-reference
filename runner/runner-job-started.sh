#!/usr/bin/env bash
# What a runner of ours does before every job, so that a job starts from what a
# hosted job starts from.
#
# A hosted runner is destroyed after one job; ours is not, and its work directory
# is whatever the last job left: a checkout at another commit, a `target` compiled
# from it, a `node_modules` from an older lockfile. Cached restores come back the
# same either way; this is the unkeyed remainder, and the difference between
# green here and red from a clean clone.
#
# Named by ACTIONS_RUNNER_HOOK_JOB_STARTED. It cannot live in the checkout,
# because the checkout is what it deletes; `docs/ci-runners.md` says where the
# copy goes.
set -euo pipefail

# Nothing outside the runner's own work directory, ever. A runner that did not say
# where that is gets nothing deleted rather than something.
inside_work() {
    case "${1:-}" in
        */_work/*) return 0 ;;
        *) echo "runner-job-started: refusing to clean '${1:-<unset>}'" >&2; return 1 ;;
    esac
}

# Empty a directory, keeping the directory and anything named in $2.
# Twice, because a job that ran in a container ran as root inside it and left
# files owned by root. The runner cannot recurse into those, so the first pass
# stops at the first one; root in a container is the one thing that can remove them.
empty() {
    local dir=$1 keep=${2:-}
    local -a except=()
    [ -n "$keep" ] && except=(! -name "$keep")

    find "$dir" -mindepth 1 -maxdepth 1 "${except[@]}" -exec rm -rf -- {} + 2>/dev/null || true
    if [ -n "$(find "$dir" -mindepth 1 -maxdepth 1 "${except[@]}" -print -quit)" ]; then
        docker run --rm --volume "$dir:/target" --entrypoint sh alpine:3 -c \
            "find /target -mindepth 1 -maxdepth 1 ${keep:+! -name '$keep'} -exec rm -rf -- {} +" \
            >/dev/null 2>&1 || true
    fi
    [ -z "$(find "$dir" -mindepth 1 -maxdepth 1 "${except[@]}" -print -quit)" ]
}

# The contents of the checkout directory, not the directory. The runner
# creates it during job setup, which happens before this script runs, and does
# not create it a second time: removing it is every action in the job failing
# to start because its working directory is gone.
#
# This one is worth failing the job over. A job that runs on the last job's
# checkout is a job whose result means nothing, and saying so is better than a
# green mark nobody can trust.
if inside_work "${GITHUB_WORKSPACE:-}" && [ -d "${GITHUB_WORKSPACE}" ]; then
    empty "$GITHUB_WORKSPACE" || {
        echo "runner-job-started: could not empty $GITHUB_WORKSPACE" >&2
        exit 1
    }
fi

# The temp directory's contents, but not `_runner_file_commands`: the runner
# creates it for this job before this script runs, and $GITHUB_ENV and
# $GITHUB_OUTPUT are files inside it. Deleting it fails every `>> "$GITHUB_ENV"`
# at once.
#
# A warning rather than a failure: what is left is scratch, and refusing to start
# over it is how one unremovable file stopped every job on a runner.
if inside_work "${RUNNER_TEMP:-}" && [ -d "${RUNNER_TEMP}" ]; then
    empty "$RUNNER_TEMP" _runner_file_commands \
        || echo "runner-job-started: $RUNNER_TEMP still holds something" >&2
fi

# Not the tool cache. A hosted image ships one, so keeping ours matches it, and
# rebuilding a JDK and a Node for every job would be slower than the job.

# The runner's mirror of this repository's main, which a full-history checkout
# borrows through alternates so it downloads the commits since the last job and
# not three thousand. Alternates only help a fetch when the mirror has refs to
# offer as "have" lines, so main is stored as a branch, not as FETCH_HEAD.
#
# Best effort: a failed fetch leaves the mirror as it was, and a stale mirror
# costs download time, never correctness. A runner already refreshing it, because
# eight start together, is left to finish.
refresh_mirror() {
    local mirror=${NZELA_GIT_MIRROR:-} url header=()
    [ -n "$mirror" ] && [ -n "${GITHUB_REPOSITORY:-}" ] || return 0
    url="${GITHUB_SERVER_URL:-https://github.com}/$GITHUB_REPOSITORY.git"
    if [ -n "${NZELA_GIT_MIRROR_TOKEN_FILE:-}" ] && [ -r "$NZELA_GIT_MIRROR_TOKEN_FILE" ]; then
        header=(-c "http.extraheader=Authorization: Basic $(printf 'x-access-token:%s' "$(cat "$NZELA_GIT_MIRROR_TOKEN_FILE")" | base64 -w0)")
    fi
    mkdir -p "$(dirname "$mirror")" || return 0
    (
        flock -n 9 || exit 0
        [ -d "$mirror/objects" ] || git init --bare -q "$mirror"
        timeout 300 git --git-dir="$mirror" "${header[@]}" fetch -q --no-tags "$url" '+refs/heads/main:refs/heads/main'
    ) 9>"$mirror.lock" || echo "runner-job-started: could not refresh $mirror" >&2
}
refresh_mirror
