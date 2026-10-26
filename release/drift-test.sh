#!/bin/bash
# What `drift.sh` promises, asserted against a stand-in Cloud Run.
#
#   tools/nzfleet/release/drift-test.sh
#
# Only the deployed side is a stand-in. What Terraform declares is read from the
# real `deploy/terraform`, because a fixture of it is one more copy to fall behind
# and falling behind is the subject. A service missing a variable is a state
# production cannot be asked for.
#
# What the stand-in cannot check is the field a describe puts an environment
# under; that shape is asserted in the stub alone.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
drift="$here/drift.sh"
root="$(cd -- "$here/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=""

export REGION=somewhere PROJECT=some-project
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
# Enough of `gcloud run … describe` for a drift check: a file of NAME=value per
# workload, one nominated workload that refuses, and the two shapes the real
# thing puts a container under. $2 is services or jobs, $3 the verb, $4 the name.
kind=$2 verb=$3 name=$4
if [ "$verb" != describe ]; then
    echo "the stub was asked for $verb, which a drift check does not do" >&2
    exit 2
fi
case " $* " in
    *" --project some-project "*) ;;
    *) echo "ERROR: (gcloud.run.$kind.describe) The required property [project] is not currently set." >&2; exit 1 ;;
esac
echo "$name" >> "$STATE/read"
[ "${REFUSE:-}" = "$name" ] && exit 1
[ -f "$STATE/$name.env" ] || exit 1

env_json="$(awk '{ i = index($0, "="); n = substr($0, 1, i - 1); v = substr($0, i + 1)
    if (NR > 1) printf ","
    if (v == "<secret>")
        printf "{\"name\":\"%s\",\"valueFrom\":{\"secretKeyRef\":{\"key\":\"latest\",\"name\":\"a-secret\"}}}", n
    else
        printf "{\"name\":\"%s\",\"value\":\"%s\"}", n, v }' "$STATE/$name.env")"

if [ "$kind" = services ]; then
    printf '{"spec":{"template":{"spec":{"containers":[{"env":[%s]}]}}}}\n' "$env_json"
else
    printf '{"spec":{"template":{"spec":{"template":{"spec":{"containers":[{"env":[%s]}]}}}}}}\n' "$env_json"
fi
STUB
chmod +x "$work/bin/gcloud"

DECLARED="$("$drift" declared)"
WORKLOADS="$(printf '%s\n' "$DECLARED" | awk '$1 != "!" { print $1 }' | sort -u)"

# A Cloud Run with nothing on it, named after the property about to use it so a
# failure says which run left what behind. The summary goes to a file of this
# run's own, both to read it back and to keep a property out of the summary of
# the ci job running it.
environment() {
    STATE="$work/state-$1"
    mkdir -p "$STATE"
    GITHUB_STEP_SUMMARY="$work/summary-$1"
    : > "$GITHUB_STEP_SUMMARY"
    export STATE GITHUB_STEP_SUMMARY
    unset REFUSE
}

# Every workload carrying exactly what Terraform requires of it, which is the
# state a deployment somebody has just applied is in. Values Terraform states
# are those; values it leaves to an expression are anything, since a check that
# cannot read them must not mind what they are.
whole() {
    local name
    for name in $WORKLOADS; do
        printf '%s\n' "$DECLARED" | awk -v n="$name" '$1 == n && $3 == "required" {
            v = $5; for (i = 6; i <= NF; i++) v = v " " $i
            print $4 "=" (v == "?" ? "whatever" : v) }' > "$STATE/$name.env"
    done
}

without() { grep -v "^$2=" "$STATE/$1.env" > "$STATE/$1.tmp" && mv "$STATE/$1.tmp" "$STATE/$1.env"; }
carrying() { printf '%s\n' "$2" >> "$STATE/$1.env"; }
instead() { without "$1" "${2%%=*}"; carrying "$1" "$2"; }

run() { ( cd "$root" && "$drift" "$@" ); }

a_deployment_that_carries_every_declaration_is_not_drift() {
    environment "$1"
    whole
    carrying nzela-api "DATABASE_URL=<secret>"
    carrying nzela-api "HONEYCOIN_API_KEY=<secret>"
    run check >/dev/null 2>&1 \
        || fail "$1" "a deployment carrying every declaration was called drift"
}

# The refusal of 2026-09-23: the image demanded SANCTIONS_MAX_AGE, Terraform had
# declared it for weeks, and the service did not have it.
a_variable_terraform_declares_and_the_service_lacks_is_drift() {
    environment "$1"
    whole
    without nzela-api SANCTIONS_MAX_AGE
    if run check >/dev/null 2>&1; then
        fail "$1" "nzela-api without SANCTIONS_MAX_AGE read as what Terraform declares"
    fi
    grep -q "nzela-api has no SANCTIONS_MAX_AGE" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the report does not name the variable that is missing"
}

# The failure #296 was opened for: seven countries arrived as keys of a map, and
# a `for_each` over it is where a variable hides from a list maintained by hand.
a_ceiling_the_map_declares_and_the_service_lacks_is_drift() {
    environment "$1"
    whole
    without nzela-sweeper FLOAT_FEE_CEILING_BIF_MINOR
    if run check >/dev/null 2>&1; then
        fail "$1" "a sweeper missing a ceiling the map declares read as whole"
    fi
}

a_value_the_deployment_contradicts_is_drift() {
    environment "$1"
    whole
    instead nzela-api "SANCTIONS_MAX_AGE=24h"
    if run check >/dev/null 2>&1; then
        fail "$1" "SANCTIONS_MAX_AGE=24h read as the 48h Terraform declares"
    fi
}

# An apply would take it away, so a variable set by hand is drift and not a
# spare. It is also the only trace a hand patch leaves.
a_variable_the_deployment_has_and_terraform_does_not_declare_is_drift() {
    environment "$1"
    whole
    carrying nzela-api "SANCTIONS_MATCH_THRESHOLD=0.5"
    if run check >/dev/null 2>&1; then
        fail "$1" "a variable nothing declares read as part of the deployment"
    fi
}

# Whether a secret is mounted depends on values in a tfvars file this never
# sees, so a mount is never required. The webhook secret is unset in the test
# environment today and the deployment is not drifted for it.
a_secret_the_deployment_does_not_mount_is_not_drift() {
    environment "$1"
    whole
    run check >/dev/null 2>&1 \
        || fail "$1" "a deployment mounting no optional secret was called drift"
}

a_secret_the_deployment_does_mount_is_not_drift() {
    environment "$1"
    whole
    carrying nzela-sweeper "OXR_APP_ID=<secret>"
    carrying nzela-sweeper "BLUEFIN_WEBHOOK_SECRET=<secret>"
    run check >/dev/null 2>&1 \
        || fail "$1" "a mounted secret Terraform declares read as undeclared"
}

# The bug #496 was: a gate that printed its complaint and passed. An
# environment that cannot be read is not an environment known to be right.
a_workload_that_cannot_be_read_is_not_a_clean_report() {
    environment "$1"
    whole
    export REFUSE=nzela-sweeper
    if run check >/dev/null 2>&1; then
        fail "$1" "a sweeper nothing could describe read as carrying its declaration"
    fi
    grep -q "nzela-sweeper could not be read" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the report does not say which workload could not be read"
}

# #571: every report said "drifted" over runs where gcloud had no project, so
# the issue told someone to apply a deployment nobody had looked at.
a_workload_that_cannot_be_read_is_unknown_and_not_drift() {
    environment "$1"
    whole
    export REFUSE=nzela-sweeper
    run check >/dev/null 2>&1
    [ $? -eq 3 ] || fail "$1" "an unreadable workload did not exit 3"
    grep -q "has drifted" "$GITHUB_STEP_SUMMARY" \
        && fail "$1" "a workload nobody could read was reported as drifted"
    grep -q "not a finding of drift" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the summary does not say this is not a finding of drift"
    return 0
}

a_measured_mismatch_is_drift_even_when_another_workload_is_unreadable() {
    environment "$1"
    whole
    without nzela-api SANCTIONS_MAX_AGE
    export REFUSE=nzela-sweeper
    run check >/dev/null 2>&1
    [ $? -eq 1 ] || fail "$1" "a measured mismatch beside an unreadable workload did not exit 1"
}

a_check_with_no_project_is_unknown_and_reads_nothing() {
    environment "$1"
    whole
    ( cd "$root" && env -u PROJECT "$drift" check >/dev/null 2>&1 )
    [ $? -eq 3 ] || fail "$1" "a check with no project did not exit 3"
    [ -f "$STATE/read" ] && fail "$1" "it asked gcloud anyway, with no project to ask about"
    return 0
}

a_drifted_deployment_exits_one() {
    environment "$1"
    whole
    without nzela-api SANCTIONS_MAX_AGE
    run check >/dev/null 2>&1
    [ $? -eq 1 ] || fail "$1" "drift did not exit 1"
}

every_workload_terraform_declares_is_one_the_check_reads() {
    environment "$1"
    whole
    run check >/dev/null 2>&1
    [ "$(sort -u "$STATE/read")" = "$(printf '%s\n' "$WORKLOADS")" ] || {
        fail "$1" "read: $(sort -u "$STATE/read" | tr '\n' ' ')"
        fail "$1" "declared: $(printf '%s ' $WORKLOADS)"
    }
}

# Refused for the right reason: the console and the site are Cloud Run services
# nobody ever put in Terraform, and a check that reported them as drifted would
# be reporting on a declaration that does not exist.
a_workload_terraform_does_not_declare_is_refused() {
    environment "$1"
    whole
    if run check nzela-console 2>"$STATE/why" >/dev/null; then
        fail "$1" "a workload Terraform declares nowhere was checked against it anyway"
    fi
    grep -q "declares no workload called nzela-console" "$STATE/why" \
        || fail "$1" "the refusal does not say that nothing declares nzela-console"
    [ -f "$STATE/read" ] \
        && fail "$1" "it described nzela-console although it had no declaration to hold it to"
    return 0
}

# The one way this can be wrong and look right: a block it cannot read, skipped.
# Terraform grows expressions and this parser does not, so the day it meets one
# it has to stop rather than report on what it happened to understand.
a_for_each_this_check_cannot_read_stops_it() {
    environment "$1"
    mkdir -p "$work/unreadable"
    cat > "$work/unreadable/services.tf" <<'TF'
resource "google_cloud_run_v2_service" "api" {
  name = "nzela-api"
  template {
    containers {
      dynamic "env" {
        for_each = local.whatever_comes_next
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}
TF
    whole
    if NZELA_TERRAFORM="$work/unreadable" run check >/dev/null 2>&1; then
        fail "$1" "a for_each it cannot evaluate was passed over rather than refused"
    fi
    [ -f "$STATE/read" ] \
        && fail "$1" "it described a workload although it could not say what to expect"
}

# The other way a check reports on nothing and calls it agreement: a
# declaration it never read. An empty answer is not an environment that agrees.
a_terraform_that_declares_nothing_is_not_a_clean_report() {
    environment "$1"
    mkdir -p "$work/nothing"
    whole
    if NZELA_TERRAFORM="$work/nothing" run check >/dev/null 2>&1; then
        fail "$1" "a declaration with no workload in it read as a deployment that agrees"
    fi
}

# The gate as the workflow runs it, rather than as this file would like to. The
# command is the block out of drift.yaml and the shell is the one GitHub gives a
# `run:`, which is `bash -e` and no `pipefail`.
step_cmd() {
    awk '/- name: What is running is what deploy\/terraform declares/ { f = 1; next }
         f && /^      - / { exit }
         f && g { sub(/^          /, ""); print }
         f && /^ *run: \|/ { g = 1 }' "$root/.github/workflows/drift.yaml"
}

ran_the_step() {
    local cmd
    cmd="$(step_cmd)"
    [ -n "$cmd" ] || return 2
    mkdir -p "$work/runner-$1"
    ( cd "$root" && GITHUB_OUTPUT="$work/runner-$1/output" RUNNER_TEMP="$work/runner-$1" bash -e -c "$cmd" )
}

a_drifted_deployment_fails_the_step_the_workflow_runs() {
    environment "$1"
    whole
    without nzela-api SANCTIONS_MAX_AGE
    ran_the_step "$1" >/dev/null 2>&1
    case $? in
        0) fail "$1" "the step the workflow runs exited 0 on a deployment that had drifted" ;;
        2) fail "$1" "drift.yaml has no run block for the step this holds" ;;
    esac
    grep -qx "status=1" "$work/runner-$1/output" \
        || fail "$1" "the step did not hand the exit status on, so the issue cannot tell drift from unknown"
}

a_whole_deployment_passes_the_step_the_workflow_runs() {
    environment "$1"
    whole
    ran_the_step "$1" >/dev/null 2>&1 \
        || fail "$1" "the step the workflow runs refused a deployment carrying every declaration"
}

# The status is the verdict and the summary is the diagnosis; the run that fails
# is the run that needs both.
the_verdict_on_drift_reaches_the_run_summary() {
    environment "$1"
    whole
    without nzela-sweeper FLOAT_FEE_CEILING_TZS_MINOR
    run check >/dev/null 2>&1
    grep -q "nzela-sweeper has no FLOAT_FEE_CEILING_TZS_MINOR" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the summary does not name the variable the sweeper lacks"
    grep -q "the deployment has drifted" "$GITHUB_STEP_SUMMARY" \
        || fail "$1" "the summary does not say the deployment has drifted"
}

# Spot checks on the reading itself, because every property above is only as
# true as the declaration it starts from.
the_declaration_is_the_one_deploy_terraform_states() {
    local ceilings n
    grep -qx "nzela-api service required SANCTIONS_MAX_AGE 48h" <<<"$DECLARED" \
        || fail "$1" "nzela-api is not read as requiring SANCTIONS_MAX_AGE 48h"
    grep -qx "nzela-api service optional DATABASE_URL ?" <<<"$DECLARED" \
        || fail "$1" "DATABASE_URL is not read as a mount nzela-api may carry"

    ceilings="$(grep -cE '^ *FLOAT_FEE_CEILING_[A-Z]+_MINOR *=' "$root/deploy/terraform/variables.tf")"
    n="$(printf '%s\n' "$DECLARED" | grep -c "^nzela-sweeper service required FLOAT_FEE_CEILING_")"
    [ "$ceilings" -gt 0 ] && [ "$n" = "$ceilings" ] \
        || fail "$1" "the map declares $ceilings ceilings and the sweeper is held to $n"

    n="$(grep -hcE '^resource "google_cloud_run_v2_(service|job)"' "$root"/deploy/terraform/*.tf |
        awk '{ total += $1 } END { print total }')"
    [ "$(printf '%s\n' "$WORKLOADS" | wc -l)" = "$n" ] \
        || fail "$1" "deploy/terraform declares $n Cloud Run workloads and this reads $(printf '%s\n' "$WORKLOADS" | wc -l)"
}

# #443 is open over the rights the deploy already holds. A daily job that reads
# four descriptions is not where they get shared out further.
the_workflow_runs_as_a_reader_and_not_as_the_deployer() {
    local yaml
    # What it does, not what it says about what it does: the file explains why
    # it is not the deployer, and that sentence is not an authentication.
    yaml="$(grep -vE '^[[:space:]]*#' "$root/.github/workflows/drift.yaml")"
    grep -q 'GCP_VERIFY_SA' <<<"$yaml" \
        || fail "$1" "drift.yaml does not name the read-only account"
    grep -q 'GCP_DEPLOY_SA' <<<"$yaml" \
        && fail "$1" "drift.yaml authenticates as the deployer"
    grep -q 'pull_request' <<<"$yaml" \
        && fail "$1" "drift.yaml runs on a pull request, which is where credentials go missing"
    return 0
}

for property in \
    a_deployment_that_carries_every_declaration_is_not_drift \
    a_variable_terraform_declares_and_the_service_lacks_is_drift \
    a_ceiling_the_map_declares_and_the_service_lacks_is_drift \
    a_value_the_deployment_contradicts_is_drift \
    a_variable_the_deployment_has_and_terraform_does_not_declare_is_drift \
    a_secret_the_deployment_does_not_mount_is_not_drift \
    a_secret_the_deployment_does_mount_is_not_drift \
    a_workload_that_cannot_be_read_is_not_a_clean_report \
    a_workload_that_cannot_be_read_is_unknown_and_not_drift \
    a_measured_mismatch_is_drift_even_when_another_workload_is_unreadable \
    a_check_with_no_project_is_unknown_and_reads_nothing \
    a_drifted_deployment_exits_one \
    every_workload_terraform_declares_is_one_the_check_reads \
    a_workload_terraform_does_not_declare_is_refused \
    a_for_each_this_check_cannot_read_stops_it \
    a_terraform_that_declares_nothing_is_not_a_clean_report \
    a_drifted_deployment_fails_the_step_the_workflow_runs \
    a_whole_deployment_passes_the_step_the_workflow_runs \
    the_verdict_on_drift_reaches_the_run_summary \
    the_declaration_is_the_one_deploy_terraform_states \
    the_workflow_runs_as_a_reader_and_not_as_the_deployer
do
    echo "$property"
    "$property" "$property"
done

if [ -n "$failed" ]; then
    printf '\nfailed:%s\n' "$failed" >&2
    exit 1
fi
