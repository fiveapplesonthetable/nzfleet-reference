#!/bin/bash
#
# What a deploy decides, out of the workflow so it can be run and tested.
#
#   REGION=... tools/nzfleet/release/release.sh components
#   REGION=... tools/nzfleet/release/release.sh ordered  <sha> [name...]
#   REGION=... tools/nzfleet/release/release.sh roll     <sha> <image>
#   REGION=... tools/nzfleet/release/release.sh record   service <name> <sha> <image>
#   REGION=... tools/nzfleet/release/release.sh coherent <sha> [name...]
#
# Every component records the commit it is on, which answers whether a candidate
# would put a component back (#373) and whether a run that stopped in the middle
# left components on different commits (#419). A service records it in the same
# call that moves its image; a job records it after the execution, because for a
# job a moved image is not work that was done.
#
# Every gate fails closed. An answer that cannot be read is not one that says
# nothing: a token that expired would otherwise be a release with no checks.
#
# Ancestry is read from the repository this runs in, so the caller stands in a
# checkout with history. Cloud Run is reached through deploy/gcp-run.sh, which
# keeps provider commands out of release decisions and the workflow.

set -uo pipefail

LABEL=nzela-commit
GCP_RUN="$(cd -- "$(dirname -- "$0")/../../.." && pwd)/deploy/gcp-run.sh"

# Every component a release moves, and the only list of them. `ordered` and
# `coherent` read this; tools/nzfleet/release/release-test.sh refuses a deploy.yaml that rolls
# something absent from it.
COMPONENTS=(
    "service nzela-api"
    "service nzela-sweeper"
    "service nzela-console"
    "service nzela-site"
    "job nzela-migrate"
    "job nzela-sanctions-refresh"
)

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
note() { printf '%s\n' "$*"; }

# The release's verdict, on the run page as well as in the log. Written here
# rather than piped into the summary by the workflow: a pipe puts `tee`'s exit
# status in front of this script's, and GitHub's default shell has no
# `pipefail`, so the first mixed release the gate ever saw was reported as a
# success.
said() {
    note "$*"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
}

kind_of() {
    local entry
    for entry in "${COMPONENTS[@]}"; do
        [ "${entry#* }" = "$1" ] && { printf '%s\n' "${entry%% *}"; return 0; }
    done
    fail "release: $1 is not a component of a release"
    return 1
}

named() {
    if [ "$#" -gt 0 ]; then
        printf '%s\n' "$@"
    else
        printf '%s\n' "${COMPONENTS[@]#* }"
    fi
}

# The commit a component names, empty when it names none, and non-zero when the
# answer could not be read at all — a gate that conflates those two is a gate an
# expired token switches off. A describe puts a service's label under the
# revision template it rolls and a job's under the resource.
recorded() {
    local kind=$1 name=$2 json
    json="$("$GCP_RUN" "$kind" "$name" "$REGION" describe --format=json)" || return 1
    printf '%s\n' "$json" |
        jq -r --arg k "$LABEL" \
            '(.spec.template.metadata.labels[$k]? // .metadata.labels[$k]? // "")'
}

# The image and the commit in one call, so a failure between them is not a
# state this has. A service that is serving the image has done everything its
# release asks of it, which is why this is the whole of a service's roll.
record() {
    local kind=$1 name=$2 sha=$3 image=$4
    local -a tracing_env=()
    if [ "$kind" != service ]; then
        fail "release: a job records its commit by running, so there is no record for one."
        return 1
    fi
    case "$name" in
        nzela-api|nzela-sweeper)
            if [ -z "${GCP_PROJECT_ID:-}" ]; then
                fail "release: GCP_PROJECT_ID is required to roll $name."
                return 1
            fi
            # NZELA_COMMIT is set here as well: the image may have been built by a train on a merge
            # commit, and the service reports the commit it was released from.
            tracing_env=(--update-env-vars "GCP_PROJECT_ID=$GCP_PROJECT_ID,NZELA_COMMIT=$sha")
            ;;
    esac
    "$GCP_RUN" service "$name" "$REGION" update \
        --image "$image" --update-labels "$LABEL=$sha" "${tracing_env[@]}" --quiet
}

# A job's release: the image, then the execution, then the commit. The label is
# the claim that the job did its work, and an image that moved is no evidence of
# that, so it is written last and only by a run that finished.
ran() {
    local name=$1 sha=$2 image=$3
    "$GCP_RUN" job "$name" "$REGION" update --image "$image" --quiet || return 1
    "$GCP_RUN" job "$name" "$REGION" execute --wait --quiet || return 1
    "$GCP_RUN" job "$name" "$REGION" update --update-labels "$LABEL=$sha" --quiet
}

# Whether $1 is an ancestor of $2. A shallow clone holds one commit, so this
# deepens it until the answer is certain: a "no" read off a truncated history
# proves nothing, a "yes" does. The deployed commit is usually a few merges back,
# so the first fetch answers it; only a refusal pays for the whole history.
reaches() {
    local older=$1 newer=$2 step
    if [ "$(git rev-parse --is-shallow-repository)" != true ]; then
        git merge-base --is-ancestor "$older" "$newer"
        return
    fi
    for step in 32 256 2048; do
        git merge-base --is-ancestor "$older" "$newer" && return 0
        git fetch -q --no-tags --deepen="$step" origin "$newer" || return 1
        [ "$(git rev-parse --is-shallow-repository)" = true ] || break
    done
    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
        git fetch -q --no-tags --unshallow origin || return 1
    fi
    git merge-base --is-ancestor "$older" "$newer"
}

# A shallow clone is missing the deployed commit until it is asked for by name.
have_commit() {
    git cat-file -e "$1^{commit}" 2>/dev/null && return 0
    [ "$(git rev-parse --is-shallow-repository)" = true ] &&
        git fetch -q --no-tags --depth=1 origin "$1" 2>/dev/null &&
        git cat-file -e "$1^{commit}" 2>/dev/null
}

# Refuse a candidate that would put a component back.
#
# Ancestry, not time: what makes a candidate safe over what is deployed is that
# it contains it, and the order two runs reach this point in says nothing about
# that. The check is against what is deployed rather than against the tip of
# main, because a newer commit whose ci is red never deploys, and refusing
# everything behind it would hold the environment on an older release to
# protect it from one that is not coming.
ordered() {
    local sha=$1 name kind here back=0 blind=0 unknown=0
    shift
    while read -r name; do
        kind="$(kind_of "$name")" || return 1
        if ! here="$(recorded "$kind" "$name")"; then
            fail "$name cannot be read, so what it is on is unknown."
            blind=1
        elif [ -z "$here" ]; then
            note "$name names no commit, so there is nothing to be older than"
        elif ! have_commit "$here"; then
            fail "$name names $here, which is not a commit here; refusing rollout."
            unknown=1
        elif [ "$here" = "$sha" ]; then
            note "$name is already on $sha"
        elif reaches "$here" "$sha"; then
            note "$name is on $here, which $sha contains"
        else
            fail "$name is on $here, and $sha does not contain it."
            back=1
        fi
    done < <(named "$@")
    [ "$blind" -eq 0 ] ||
        echo "A component nobody can read may be on a release this one would undo." >&2
    [ "$unknown" -eq 0 ] ||
        echo "Fetch and verify the deployed commit before retrying this release." >&2
    [ "$back" -eq 0 ] ||
        echo "A newer release is deployed. This one is superseded and must not roll." >&2
    [ "$blind" -eq 0 ] && [ "$unknown" -eq 0 ] && [ "$back" -eq 0 ]
}

# The API side of a release, in the order the schema demands. Every step is
# idempotent, so a re-run of a failed run completes the release rather than
# starting a second one, and `coherent` is what says whether it got there.
roll() {
    local sha=$1 image=$2
    ran nzela-migrate "$sha" "$image" || return 1
    record service nzela-api "$sha" "$image" || return 1
    record service nzela-sweeper "$sha" "$image" || return 1
    # A deployment whose lists are empty screens nobody: `screen` answers
    # `NoList`, which is `503 not_screenable`, and every confirm and every
    # verification verdict is refused until the daily schedule fires. Run after
    # the services so it fetches with the image this release deployed.
    ran nzela-sanctions-refresh "$sha" "$image"
}

# Every component named is on the same commit. A component that names none
# fails this: the run that rolled it wrote the label itself, so reading nothing
# back means the recording is broken, and a release nobody can identify is the
# state this exists to refuse.
coherent() {
    local sha=$1 name kind here bad=0
    shift
    while read -r name; do
        kind="$(kind_of "$name")" || return 1
        if ! here="$(recorded "$kind" "$name")"; then
            said "$name cannot be read, so what it is on is unknown"
            bad=1
        elif [ "$here" = "$sha" ]; then
            said "$name is on $sha"
        else
            # In the summary with the rest: a list that silently omits the
            # components that are wrong is the list nobody needed.
            said "$name is on ${here:-nothing}, and this release is $sha"
            bad=1
        fi
    done < <(named "$@")
    [ "$bad" -eq 0 ] && return 0
    said "the release is mixed"
    echo "Re-run this workflow: every step is idempotent" >&2
    echo "and a second run completes the components the first did not reach." >&2
    return 1
}

[ -n "${REGION:-}" ] || { fail "release: REGION is unset."; exit 2; }

case "${1:-}" in
    components) printf '%s\n' "${COMPONENTS[@]}" ;;
    ordered)    shift; ordered "$@" ;;
    roll)       shift; roll "$@" ;;
    record)     shift; record "$@" ;;
    coherent)   shift; coherent "$@" ;;
    *)
        echo "usage: tools/nzfleet/release/release.sh components|ordered|roll|record|coherent" >&2
        exit 2
        ;;
esac
