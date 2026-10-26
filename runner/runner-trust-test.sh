#!/bin/bash
# Which machine a workflow is allowed to ask for.
#
#   tools/nzfleet/runner/runner-trust-test.sh
#
# Our runners are two tiers on one machine and the Unix account is the whole of
# the boundary between them, so what chooses a tier has to be something nobody
# outside the team can write. `docs/ci-runners.md` has the arrangement. These are
# the two shapes that would undo it, and neither is visible in a diff that only
# reads the job it changed.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
failed=""

fail() {
    case " $failed " in
        *" $1 "*) ;;
        *) failed="$failed $1" ;;
    esac
    shift
    echo "  $*" >&2
}

workflows() {
    find "$root/.github/workflows" -maxdepth 1 \( -name '*.yaml' -o -name '*.yml' \) | sort
}

# The `on:` key and what is indented under it, which is where a file says what
# starts it.
triggers() {
    awk '/^on:/ { inside = 1; print; next }
         inside && /^[^[:space:]#]/ { exit }
         inside { print }' "$1"
}

# A label GitHub builds a machine for and destroys afterwards. Anchored at the
# start: `${{ vars.CI_RUNS_ON || 'ubuntu-latest' }}` holds one of these names and
# is not one, and a substring match would read every job as hosted.
hosted() {
    case "$1" in
        ubuntu-*|windows-*|macos-*) return 0 ;;
        *) return 1 ;;
    esac
}

# The value, without the key or the space between them.
value_of() {
    local rest=${1#*:}
    printf '%s' "${rest#"${rest%%[![:space:]]*}"}"
}

# The one `runs-on` a pull request run may use besides GitHub's own machines:
# the pull-request tier, named by a constant because a fork's pull request sees no
# repository variables. A
# pull request writes its own `runs-on`, so this test cannot stop one that
# ignores it; what does is the trusted tier refusing any workflow that is not
# main's. `docs/ci-runners.md` has that setting.
PR_TIER="\${{ github.event_name == 'pull_request' && 'nzela-pr' || inputs.full && vars.CI_POSTSUBMIT_RUNS_ON || vars.CI_RUNS_ON || 'ubuntu-latest' }}"
# Only reached by a job whose `if` keeps pull requests out, checked in
# tools/nzfleet/deploycheck/workflow-concurrency-test.sh.
DEVICE_TIER="\${{ vars.CI_DEVICE_RUNS_ON || vars.CI_RUNS_ON || 'ubuntu-latest' }}"

# A file triggered by a pull request asks only for hosted machines or the
# expressions above.
a_workflow_a_pull_request_defines_runs_on_only_the_pull_request_tier() {
    local f line
    local -a asked
    while read -r f; do
        triggers "$f" | grep -q pull_request || continue
        asked=()
        while read -r line; do
            line="$(value_of "$line")"
            hosted "$line" || [ "$line" = "$PR_TIER" ] || [ "$line" = "$DEVICE_TIER" ] || asked+=("$line")
        done < <(grep -E '^[[:space:]]*runs-on:' "$f")
        [ ${#asked[@]} -eq 0 ] || fail "$1" \
            "$(basename "$f") is started by a pull request and asks for $(printf '%s\n' "${asked[@]}" | sort -u | paste -sd', ')"
    done < <(workflows)
}

# A manual dispatch can select a branch, and that branch supplies the workflow
# definition. A persistent runner is not a safe target for workflow code chosen
# this way, even when the default branch's copy asks for GitHub's runner.
a_workflow_dispatch_uses_only_githubs_machines() {
    local f line
    local -a asked
    while read -r f; do
        triggers "$f" | grep -Eq '^[[:space:]]*workflow_dispatch:[[:space:]]*($|#)' || continue
        asked=()
        while read -r line; do
            line="$(value_of "$line")"
            hosted "$line" || asked+=("$line")
        done < <(grep -E '^[[:space:]]*runs-on:' "$f")
        [ ${#asked[@]} -eq 0 ] || fail "$1" \
            "$(basename "$f") can be dispatched from a branch and asks for persistent runner $(printf '%s\n' "${asked[@]}" | sort -u | paste -sd', ')"
    done < <(workflows)
}

# A secret is a file on the machine for as long as the job runs, and ours is a
# machine that is still there afterwards. The Android upload key is the one
# credential here that cannot be rotated, so the rule is the shape rather than
# the name: a job that reads `secrets.` names its own hosted runner.
a_job_that_reads_a_secret_runs_where_the_machine_does_not_outlive_it() {
    local f job on
    while read -r f; do
        while IFS='	' read -r job on; do
            hosted "$on" || fail "$1" "$(basename "$f") gives $job a secret and runs it on $on"
        done < <(awk '
            /^jobs:/ { inside = 1; next }
            !inside { next }
            /^[[:space:]]*#/ { next }
            /^  [^ ]/ { job = $1; sub(":$", "", job) }
            /^[[:space:]]*runs-on:/ { sub(/^[^:]*:[[:space:]]*/, ""); on[job] = $0 }
            /secrets\./ { reads[job] = 1 }
            END { for (j in reads) printf "%s\t%s\n", j, on[j] }' "$f")
    done < <(workflows)
}

for property in \
    a_workflow_a_pull_request_defines_runs_on_only_the_pull_request_tier \
    a_workflow_dispatch_uses_only_githubs_machines \
    a_job_that_reads_a_secret_runs_where_the_machine_does_not_outlive_it
do
    echo "$property"
    "$property" "$property"
done

if [ -n "$failed" ]; then
    printf '\nfailed:%s\n' "$failed" >&2
    exit 1
fi
