#!/bin/bash
# What `release.sh` promises, asserted against a stand-in Cloud Run.
#
#   tools/nzfleet/release/release-test.sh
#
# The stand-in keeps each component's commit in a file and fails whichever call it
# is told to, which reproduces a run that stops in the middle rather than waiting
# for one in production. The shape of the field a describe puts a label under is
# asserted in the stub alone.

set -uo pipefail

# `git push` hands its hooks a GIT_DIR, and the pre-push hook runs this. Without
# unsetting it `git init` re-opens the repository being pushed and the fixture's
# commits land on the branch under test.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE GIT_PREFIX

here="$(cd -- "$(dirname -- "$0")" && pwd)"
release="$here/release.sh"
root="$(cd -- "$here/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=""

export REGION=somewhere
export PATH="$work/bin:$PATH"

fail() {
    case " $failed " in
        *" $1 "*) ;;
        *) failed="$failed $1" ;;
    esac
    shift
    echo "  $*" >&2
}

mkdir -p "$work/bin"
cat > "$work/bin/gcloud" <<'STUB'
#!/bin/bash
# Enough of `gcloud run` for a release: a label per component, a counter, and a
# nominated call that refuses — by number in FAIL_AT, or by "$verb $name" in
# FAIL_CALL. $2 is services or jobs, $3 the verb, $4 the name.
kind=$2 verb=$3 name=$4
shift 4
image="" commit="" project_id="" runtime_commit=""
while [ $# -gt 0 ]; do
    case $1 in
        --image)          image=$2; shift 2 ;;
        --update-labels)  commit=${2#*=}; shift 2 ;;
        --update-env-vars) project_id=${2#GCP_PROJECT_ID=}; runtime_commit=${2##*NZELA_COMMIT=}; project_id=${project_id%%,*}; shift 2 ;;
        *)                shift ;;
    esac
done

case $verb in
    describe)
        [ -f "$STATE/$name.commit" ] || { echo '{}'; exit 0; }
        # A service's label is read off the revision template and a job's off
        # the resource, which is the one shape of the real thing this asserts.
        if [ "$kind" = services ]; then
            printf '{"spec":{"template":{"metadata":{"labels":{"nzela-commit":"%s"}}}}}\n' \
                "$(cat "$STATE/$name.commit")"
        else
            printf '{"metadata":{"labels":{"nzela-commit":"%s"}}}\n' \
                "$(cat "$STATE/$name.commit")"
        fi
        ;;
    update|execute)
        n=$(( $(cat "$STATE/calls" 2>/dev/null || echo 0) + 1 ))
        echo "$n" > "$STATE/calls"
        echo "$verb $name" >> "$STATE/log"
        if [ "${FAIL_AT:-0}" = "$n" ] || [ "${FAIL_CALL:-}" = "$verb $name" ]; then
            echo "gcloud: $verb $name refused" >&2
            exit 1
        fi
        # An update writes only what it was handed. An `update --image` with no
        # `--update-labels` leaves the label where it was, which is what makes
        # moving a job's image and claiming the job ran two separable things.
        if [ "$verb" = update ]; then
            [ -z "$commit" ] || printf %s "$commit" > "$STATE/$name.commit"
            [ -z "$image" ]  || printf %s "$image"  > "$STATE/$name.image"
            [ -z "$project_id" ] || printf %s "$project_id" > "$STATE/$name.project"
            [ -z "$runtime_commit" ] || printf %s "$runtime_commit" > "$STATE/$name.runtime-commit"
        fi
        ;;
    *)
        echo "the stub was asked for $verb, which a release does not do" >&2
        exit 2
        ;;
esac
STUB
chmod +x "$work/bin/gcloud"

# A repository with two commits on main and one on a branch that left before
# them, which is every ancestry a deploy has to judge.
git init -q -b main "$work/repo"
git -C "$work/repo" -c user.name=t -c user.email=t@example.com \
    commit -q --allow-empty -m first
OLD="$(git -C "$work/repo" rev-parse HEAD)"
git -C "$work/repo" -c user.name=t -c user.email=t@example.com \
    commit -q --allow-empty -m second
NEW="$(git -C "$work/repo" rev-parse HEAD)"
git -C "$work/repo" checkout -q -b elsewhere "$OLD"
git -C "$work/repo" -c user.name=t -c user.email=t@example.com \
    commit -q --allow-empty -m aside
ASIDE="$(git -C "$work/repo" rev-parse HEAD)"
git -C "$work/repo" checkout -q main

# A Cloud Run with nothing on it, named after the property that is about to use
# it so a failure says which run left what behind. The summary goes to a file of
# this run's own, both to read it back and to keep a property out of the summary
# of the ci job running it.
environment() {
    STATE="$work/state-$1"
    mkdir -p "$STATE"
    GITHUB_STEP_SUMMARY="$work/summary-$1"
    : > "$GITHUB_STEP_SUMMARY"
    export STATE GITHUB_STEP_SUMMARY
    export GCP_PROJECT_ID=example-project
    unset FAIL_AT FAIL_CALL
}

on() { printf %s "$2" > "$STATE/$1.commit"; }

run() { ( cd "$work/repo" && "$release" "$@" ); }

# A clone holding the one commit, as the deploy's checkout leaves it.
run_shallow() {
    local at=$1 clone="$work/shallow-$1-$$"
    shift
    [ -d "$clone" ] || git clone -q --depth=1 "file://$work/repo" "$clone" 2>/dev/null
    git -C "$clone" checkout -q "$at" 2>/dev/null ||
        { git -C "$clone" fetch -q --depth=1 origin "$at" && git -C "$clone" checkout -q "$at"; }
    ( cd "$clone" && "$release" "$@" )
}

IMAGE=registry/nzela@sha256:0

# The whole list, so a component added to it is one every property covers.
ALL="$("$release" components | cut -d' ' -f2)"

# The components a roll does not move, so a property about a roll can start them
# where a real release would have left them.
UNROLLED="nzela-console nzela-site"

# How many gcloud calls a roll makes, counted off a roll that refuses nothing
# rather than written down, so a call added to a roll is a call the injection
# properties cover.
environment count
run roll "$NEW" "$IMAGE" >/dev/null 2>&1
CALLS="$(cat "$STATE/calls" 2>/dev/null || echo 0)"
# Nought would make every injection loop below run no iterations and pass.
[ "$CALLS" -gt 0 ] || { echo "a roll that nothing refused called gcloud $CALLS times" >&2; exit 1; }

a_release_names_the_commit_on_every_component_it_moved() {
    environment "$1"
    run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
        || fail "$1" "the roll did not finish against a stand-in that refuses nothing"
    for name in nzela-migrate nzela-api nzela-sweeper nzela-sanctions-refresh; do
        [ "$(cat "$STATE/$name.commit" 2>/dev/null)" = "$NEW" ] \
            || fail "$1" "$name does not name $NEW after a roll"
    done
}

a_test_release_sets_the_project_on_both_traced_services() {
    environment "$1"
    run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
        || fail "$1" "the roll did not finish while setting the project on traced services"
    for name in nzela-api nzela-sweeper; do
        [ "$(cat "$STATE/$name.project" 2>/dev/null)" = "$GCP_PROJECT_ID" ] \
            || fail "$1" "$name did not receive GCP_PROJECT_ID=$GCP_PROJECT_ID"
    done
}

a_rolled_service_reports_the_commit_it_was_released_from_not_the_one_that_built_its_image() {
    environment "$1"
    run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
        || fail "$1" "the roll did not finish while setting the runtime commit"
    for name in nzela-api nzela-sweeper; do
        [ "$(cat "$STATE/$name.runtime-commit" 2>/dev/null)" = "$NEW" ] \
            || fail "$1" "$name did not receive NZELA_COMMIT=$NEW"
    done
}

a_traced_service_refuses_to_roll_without_the_project() {
    environment "$1"
    unset GCP_PROJECT_ID
    if run record service nzela-api "$NEW" "$IMAGE" >/dev/null 2>&1; then
        fail "$1" "nzela-api rolled without GCP_PROJECT_ID"
    fi
    [ ! -f "$STATE/calls" ] \
        || fail "$1" "Cloud Run was called despite the missing GCP_PROJECT_ID"
}

the_test_deploy_passes_its_project_to_the_release() {
    local deploy_job
    deploy_job="$(awk '/^  deploy:$/ {found=1; next}
                        found && /^  [[:alnum:]_-]+:$/ {exit}
                        found {print}' "$root/.github/workflows/deploy.yaml")"
    printf '%s\n' "$deploy_job" \
        | grep -Fqx '      GCP_PROJECT_ID: ${{ vars.GCP_PROJECT }}' \
        || fail "$1" "the test deploy does not pass its GCP project to release.sh"
}

each_deploy_image_uses_the_configured_repository_prefix() {
    local image_count
    local image_prefix="\${{ vars.REGISTRY || format('{0}-docker.pkg.dev/{1}/nzela', vars.GCP_REGION, vars.GCP_PROJECT) }}"
    image_count="$(grep -Fxc "      IMAGE: $image_prefix/nzela:\${{ github.event.workflow_run.head_sha }}" "$root/.github/workflows/deploy.yaml" || true)"
    image_count=$((image_count + $(grep -Fxc "      IMAGE: $image_prefix/console:\${{ github.event.workflow_run.head_sha }}" "$root/.github/workflows/deploy.yaml" || true)))
    image_count=$((image_count + $(grep -Fxc "      IMAGE: $image_prefix/site:\${{ github.event.workflow_run.head_sha }}" "$root/.github/workflows/deploy.yaml" || true)))
    [ "$image_count" -eq 3 ] \
        || fail "$1" "the three deploy images do not use REGISTRY"
    grep -Fq 'REGISTRY         = "${var.region}-docker.pkg.dev/${var.project}/nzela"' \
        "$root/deploy/terraform/bootstrap/main.tf" \
        || fail "$1" "the bootstrap output does not retain today's registry"
    grep -Fq 'registry = var.registry != "" ? var.registry :' \
        "$root/deploy/terraform/outputs.tf" \
        || fail "$1" "the Terraform image output has no configurable registry"
}

a_newer_commit_rolls_over_the_one_deployed() {
    environment "$1"
    for name in $ALL; do on "$name" "$OLD"; done
    run ordered "$NEW" >/dev/null 2>&1 \
        || fail "$1" "$NEW was refused over $OLD, which it contains"
}

# The regression #373 is about: ci for an older merge finishing after a newer
# one has already deployed.
an_older_commit_is_refused_once_a_newer_one_has_deployed() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    if run ordered "$OLD" >/dev/null 2>&1; then
        fail "$1" "$OLD was allowed to roll over $NEW"
    fi
}

# One lagging component is enough, because it is the one that would go back.
an_older_commit_is_refused_when_a_single_component_is_ahead() {
    environment "$1"
    for name in $ALL; do on "$name" "$OLD"; done
    on nzela-site "$NEW"
    if run ordered "$OLD" >/dev/null 2>&1; then
        fail "$1" "$OLD rolled although nzela-site was on $NEW"
    fi
}

a_commit_that_does_not_contain_what_is_deployed_is_refused() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    if run ordered "$ASIDE" >/dev/null 2>&1; then
        fail "$1" "a commit off a branch that never saw $NEW was allowed to roll over it"
    fi
}

a_shallow_checkout_finds_the_deployed_commit_it_contains() {
    environment "$1"
    for name in $ALL; do on "$name" "$OLD"; done
    run_shallow "$NEW" ordered "$NEW" >/dev/null 2>&1 \
        || fail "$1" "a one-commit checkout of $NEW was refused over $OLD, which it contains"
}

a_shallow_checkout_still_refuses_an_older_commit() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    if run_shallow "$OLD" ordered "$OLD" >/dev/null 2>&1; then
        fail "$1" "a one-commit checkout of $OLD was allowed to roll over $NEW"
    fi
}

a_shallow_checkout_refuses_a_commit_off_the_deployed_branch() {
    environment "$1"
    for name in $ALL; do on "$name" "$ASIDE"; done
    if run_shallow "$NEW" ordered "$NEW" >/dev/null 2>&1; then
        fail "$1" "a one-commit checkout of $NEW rolled over $ASIDE, which it does not contain"
    fi
}

a_re_run_of_the_commit_already_deployed_is_allowed() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    run ordered "$NEW" >/dev/null 2>&1 \
        || fail "$1" "a re-run of $NEW was refused against itself"
}

a_component_that_names_no_commit_does_not_hold_up_a_release() {
    local output
    environment "$1"
    output="$(run ordered "$NEW" 2>&1)" \
        || fail "$1" "a Cloud Run that names no commit anywhere refused the first release"
    case "$output" in
        *"names no commit, so there is nothing to be older than"*) ;;
        *) fail "$1" "a missing commit did not say it names no commit" ;;
    esac
}

an_unknown_deployed_commit_refuses_a_release() {
    local unknown=0000000000000000000000000000000000000000 candidate output
    for candidate in "$NEW" "$unknown"; do
        environment "$1-$candidate"
        for name in $ALL; do on "$name" "$OLD"; done
        on nzela-api "$unknown"
        if output="$(run ordered "$candidate" 2>&1 && run roll "$candidate" "$IMAGE" 2>&1)"; then
            fail "$1" "a release rolled over an unknown deployed commit"
        fi
        [ ! -f "$STATE/calls" ] \
            || fail "$1" "a release changed Cloud Run after finding an unknown deployed commit"
        case "$output" in
            *"nzela-api names $unknown, which is not a commit here"*) ;;
            *) fail "$1" "the refusal did not identify the unknown deployed commit" ;;
        esac
    done
}

# Every call the roll makes, refused in turn, and after each one the release has
# to be visibly mixed rather than quietly finished.
#
# The console and the site start on $NEW because a roll does not move them. On
# $OLD they fail the coherence check on their own, whichever call was refused,
# and a property that would pass with no call refused at all is not asserting
# what it is named after.
a_failure_part_way_through_leaves_the_release_mixed() {
    local at
    for at in $(seq 1 "$CALLS"); do
        environment "$1-$at"
        for name in $ALL; do on "$name" "$OLD"; done
        for name in $UNROLLED; do on "$name" "$NEW"; done
        FAIL_AT=$at run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
            && fail "$1" "the roll reported success with call $at refused"
        if run coherent "$NEW" >/dev/null 2>&1; then
            fail "$1" "the release read as coherent with call $at refused"
        fi
    done
}

a_second_run_after_a_failure_completes_the_same_release() {
    local at
    for at in $(seq 1 "$CALLS"); do
        environment "$1-$at"
        for name in $ALL; do on "$name" "$OLD"; done
        for name in $UNROLLED; do on "$name" "$NEW"; done
        FAIL_AT=$at run roll "$NEW" "$IMAGE" >/dev/null 2>&1
        run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
            || fail "$1" "the second roll failed after call $at had been refused"
        run coherent "$NEW" >/dev/null 2>&1 \
            || fail "$1" "the release is still mixed after re-running a roll that failed at call $at"
    done
}

# A job's label is the claim that the job ran. A migration whose execution was
# refused has not moved the schema and a sanctions refresh that did not run
# screens nobody, so neither may name the release the image was built from.
a_job_whose_run_was_refused_does_not_name_the_release() {
    local job
    for job in nzela-migrate nzela-sanctions-refresh; do
        environment "$1-$job"
        for name in $ALL; do on "$name" "$NEW"; done
        on "$job" "$OLD"
        FAIL_CALL="execute $job" run roll "$NEW" "$IMAGE" >/dev/null 2>&1 \
            && fail "$1" "the roll reported success with $job's execution refused"
        [ "$(cat "$STATE/$job.commit" 2>/dev/null)" = "$OLD" ] \
            || fail "$1" "$job names $NEW although its execution was refused"
        if run coherent "$NEW" >/dev/null 2>&1; then
            fail "$1" "a release $job never ran for read as coherent"
        fi
    done
}

# The one call that could write a job's label without running the job is
# refused, so the defect above cannot come back through the command line.
a_jobs_commit_cannot_be_recorded_without_running_it() {
    environment "$1"
    for name in $ALL; do on "$name" "$OLD"; done
    if run record job nzela-migrate "$NEW" "$IMAGE" >/dev/null 2>&1; then
        fail "$1" "\`release.sh record job\` labelled a job that had not run"
    fi
    [ "$(cat "$STATE/nzela-migrate.commit" 2>/dev/null)" = "$OLD" ] \
        || fail "$1" "nzela-migrate names $NEW after a record that should have been refused"
}

a_release_one_component_never_reached_is_not_coherent() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    on nzela-console "$OLD"
    if run coherent "$NEW" >/dev/null 2>&1; then
        fail "$1" "a console left on $OLD read as part of the $NEW release"
    fi
}

# Reading nothing back means the label was never written, which is a release
# nobody can identify rather than a release that is fine.
a_component_that_names_no_commit_is_not_coherent() {
    environment "$1"
    for name in $ALL; do on "$name" "$NEW"; done
    rm -f "$STATE/nzela-sweeper.commit"
    if run coherent "$NEW" >/dev/null 2>&1; then
        fail "$1" "a sweeper naming no commit at all read as part of the release"
    fi
}

# Both gates, against an environment that answers nothing: an expired token, a
# region that names no deployment, a component terraform has not created, a
# runner without `jq`. `ordered` read all four as "names no commit" and rolled,
# so anything that made a describe fail took #373's protection off entirely.
every_gate_refuses_an_answer_it_cannot_read() {
    local gate broken bin
    for broken in gcloud jq; do
        bin="$work/without-$broken"
        mkdir -p "$bin"
        printf '#!/bin/bash\nexit 1\n' > "$bin/$broken"
        chmod +x "$bin/$broken"
        for gate in ordered coherent; do
            environment "$1-$gate-$broken"
            for name in $ALL; do on "$name" "$NEW"; done
            if ( cd "$work/repo" && PATH="$bin:$PATH" "$release" "$gate" "$NEW" ) \
                >/dev/null 2>&1
            then
                fail "$1" "$gate passed with a $broken that answers nothing"
            fi
        done
    done
}

# The gate as the workflow runs it, rather than as this file would like to run
# it. The command is the line out of deploy.yaml and the shell is the one
# GitHub gives a `run:`, which is `bash -e` and — the whole point — no
# `pipefail` unless the step asks for `shell: bash`.
gate_step() {
    local block
    block="$(awk '/- name: Every component is on this commit/ {f=1; print; next}
                  f && /^      - / {exit} f {print}' \
        "$root/.github/workflows/deploy.yaml")"
    STEP_CMD="$(printf '%s\n' "$block" | sed -n 's/^ *run: //p' | head -1)"
    case "$(printf '%s\n' "$block" | sed -n 's/^ *shell: //p' | head -1)" in
        bash) STEP_OPTS="-eo pipefail" ;;
        *)    STEP_OPTS="-e" ;;
    esac
}

ran_the_gate_step() { ( cd "$root" && SHA="$1" bash $STEP_OPTS -c "$STEP_CMD" ); }

# What the script decided, and what the step reported, are the same thing. They
# were not: `coherent` returned 1 into a pipe, the step took `tee`'s status, and
# the first mixed release this gate ever met went green.
a_mixed_release_fails_the_step_the_workflow_runs() {
    environment "$1"
    gate_step
    [ -n "$STEP_CMD" ] || {
        fail "$1" "deploy.yaml has no run: for the step this holds"
        return
    }
    for name in $ALL; do on "$name" "$NEW"; done
    on nzela-api "$OLD"
    if ran_the_gate_step "$NEW" >/dev/null 2>&1; then
        fail "$1" "\`$STEP_CMD\` under \`bash $STEP_OPTS\` exited 0 with nzela-api on $OLD"
    fi
}

a_whole_release_passes_the_step_the_workflow_runs() {
    environment "$1"
    gate_step
    for name in $ALL; do on "$name" "$NEW"; done
    ran_the_gate_step "$NEW" >/dev/null 2>&1 \
        || fail "$1" "\`$STEP_CMD\` refused a release every component is on"
}

# The status is the verdict and the summary is the diagnosis; a run that fails
# is the run that needs both.
the_verdict_on_a_mixed_release_reaches_the_run_summary() {
    environment "$1"
    gate_step
    for name in $ALL; do on "$name" "$NEW"; done
    on nzela-console "$OLD"
    ran_the_gate_step "$NEW" >/dev/null 2>&1
    grep -q "nzela-console is on $OLD" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the summary does not name the console as the component that lagged"
    grep -q "the release is mixed" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the summary does not say the release is mixed"
}

# The list this script holds and what actually moves an image, kept the same by
# something other than memory.
every_component_named_here_is_one_something_rolls() {
    local rolled
    rolled="$( {
        grep -oE '(record (service|job)|ran) (nzela-[a-z-]+)' "$release"
        grep -oE 'release\.sh record (service|job) (nzela-[a-z-]+)' \
            "$root/.github/workflows/deploy.yaml"
    } | grep -oE 'nzela-[a-z-]+' | sort -u)"
    [ "$rolled" = "$(printf '%s\n' $ALL | sort -u)" ] || {
        fail "$1" "these are rolled: $(echo $rolled)"
        fail "$1" "and these are named: $(echo $ALL)"
    }
}

# A check that runs from a pre-push hook is handed that repository, and this one
# builds a repository of its own. The two must not be the same one, or the
# branch being pushed is where the fixture's history lands.
this_suite_commits_nothing_to_the_repository_it_was_run_from() {
    local sandbox before after
    [ -z "${RELEASE_TEST_NESTED:-}" ] || return 0
    sandbox="$work/somebody-elses-repository"
    git init -q -b main "$sandbox"
    git -C "$sandbox" -c user.name=t -c user.email=t@example.com \
        commit -q --allow-empty -m untouched
    before="$(git -C "$sandbox" rev-parse HEAD)"
    GIT_DIR="$sandbox/.git" RELEASE_TEST_NESTED=1 "$here/release-test.sh" >/dev/null 2>&1
    after="$(git -C "$sandbox" rev-parse HEAD)"
    [ "$before" = "$after" ] \
        || fail "$1" "a run under GIT_DIR moved that repository's HEAD from $before to $after"
}

the_workflow_moves_no_image_except_through_this_script() {
    local direct
    # Anything but a comment: the file explains what a roll used to be, and a
    # sentence about it is not one.
    direct="$(grep -nE '^[[:space:]]*[^#[:space:]].*gcloud run (services|jobs) update' \
        "$root/.github/workflows/deploy.yaml" || true)"
    [ -z "$direct" ] || fail "$1" "deploy.yaml rolls something itself: $direct"
}

for property in \
    a_release_names_the_commit_on_every_component_it_moved \
    a_test_release_sets_the_project_on_both_traced_services \
    a_rolled_service_reports_the_commit_it_was_released_from_not_the_one_that_built_its_image \
    a_traced_service_refuses_to_roll_without_the_project \
    the_test_deploy_passes_its_project_to_the_release \
    each_deploy_image_uses_the_configured_repository_prefix \
    a_newer_commit_rolls_over_the_one_deployed \
    an_older_commit_is_refused_once_a_newer_one_has_deployed \
    an_older_commit_is_refused_when_a_single_component_is_ahead \
    a_commit_that_does_not_contain_what_is_deployed_is_refused \
    a_shallow_checkout_finds_the_deployed_commit_it_contains \
    a_shallow_checkout_still_refuses_an_older_commit \
    a_shallow_checkout_refuses_a_commit_off_the_deployed_branch \
    a_re_run_of_the_commit_already_deployed_is_allowed \
    a_component_that_names_no_commit_does_not_hold_up_a_release \
    an_unknown_deployed_commit_refuses_a_release \
    a_failure_part_way_through_leaves_the_release_mixed \
    a_second_run_after_a_failure_completes_the_same_release \
    a_job_whose_run_was_refused_does_not_name_the_release \
    a_jobs_commit_cannot_be_recorded_without_running_it \
    a_release_one_component_never_reached_is_not_coherent \
    a_component_that_names_no_commit_is_not_coherent \
    every_gate_refuses_an_answer_it_cannot_read \
    a_mixed_release_fails_the_step_the_workflow_runs \
    a_whole_release_passes_the_step_the_workflow_runs \
    the_verdict_on_a_mixed_release_reaches_the_run_summary \
    every_component_named_here_is_one_something_rolls \
    this_suite_commits_nothing_to_the_repository_it_was_run_from \
    the_workflow_moves_no_image_except_through_this_script
do
    echo "$property"
    "$property" "$property"
done

if [ -n "$failed" ]; then
    printf '\nfailed:%s\n' "$failed" >&2
    exit 1
fi
