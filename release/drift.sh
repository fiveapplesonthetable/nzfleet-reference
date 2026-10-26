#!/bin/bash
#
# What deploy/terraform declares, asked of what is running.
#
#   tools/nzfleet/release/drift.sh declared [workload...]
#   PROJECT=... REGION=... tools/nzfleet/release/drift.sh check [workload...]
#
# Exit 0 when every workload carries what is declared, 1 when one measurably
# does not, and 3 when a workload could not be read and nothing else
# contradicted it. The last is not drift: it says nothing is known, and the
# remedy is to fix the reading rather than to apply anything. The project is
# passed on every call rather than inherited, so a runner with no default
# project is an unreadable workload with its own name, not a silent one.
#
# Terraform owns every workload's environment and nothing applies it, so a
# variable added there sits in the repository while the running service does
# not have it. `crates/nzela-config/src` holds the code to what Terraform
# declares; this holds what is deployed to the same thing, which is the half
# that was missing the day a deploy was refused over a SANCTIONS_MAX_AGE
# Terraform had declared for weeks.
#
# It reads, and the identity it runs as cannot do anything else. Reporting
# drift rather than reconciling it is what makes that possible: an apply would
# need rights over the whole environment and would reconcile everything that
# had drifted, not the one variable somebody meant.
#
# The declaration is read out of the HCL rather than out of a plan, because a
# plan needs the values Terraform writes into Secret Manager — the payout
# provider's key among them — and a check that must hold production
# credentials to say a variable is missing costs more than it saves. That is
# exact on names and partial on values: a literal in an `env` block is
# compared and an expression is reported as unread rather than guessed at. A
# `for_each` this cannot evaluate refuses instead of passing quietly.
#
# gcloud is reached through `live` and nowhere else, which is the seam
# tools/nzfleet/release/drift-test.sh replaces.

set -uo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
TERRAFORM="${NZELA_TERRAFORM:-$HERE/../../../deploy/terraform}"

fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

# The verdict on the run page as well as in the log, and written here rather
# than piped into the summary: a pipeline's status is its last stage's and
# GitHub's default shell has no `pipefail`, so `| tee` once reported a release
# as whole that the script had called mixed.
said() {
    printf '%s\n' "$*"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
}

# Every environment variable deploy/terraform states, as
# `<workload> <kind> required|optional <NAME> <value|?>`, and a line beginning
# `!` for each expression this could not read.
#
# Required is an `env` block that is always there; optional is a secret mount
# or a block behind a condition, whose presence depends on values that live in
# a tfvars file this never sees. A value is compared only where the HCL states
# a literal, because `local.container_cpu` is a number this would have to be
# Terraform to know and a wrong guess is a check nobody believes.
declared() {
    awk '
    function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t\r]+$/, "", s); return s }
    function quoted(s,   n) { n = index(s, "\""); if (n == 0) return ""
        s = substr(s, n + 1); n = index(s, "\""); if (n == 0) return ""
        return substr(s, 1, n - 1) }
    function rhs(s) { sub(/^[^=]*=[ \t]*/, "", s); return trim(s) }
    function key_of(s) { sub(/[ \t]*=.*$/, "", s); return s }
    function say(need, name, value) {
        if (name != "") printf "%s %s %s %s %s\n", workload, kind, need, name, value }

    { line = trim($0) }
    line ~ /^#/ { next }

    # A map variable whose keys are the variable names it sets. The deployment
    # says every variable it sets, and this is where it says the ones a
    # `for_each` would otherwise hide.
    pass == 1 {
        if (line ~ /^variable "/) { var = quoted(line); indefault = 0 }
        if (var != "" && line ~ /^default[ \t]*=[ \t]*\{/) { indefault = 1; next }
        if (indefault) {
            if (line ~ /^\}/) { indefault = 0; next }
            if (line ~ /^[A-Z][A-Z0-9_]*[ \t]*=/) map[var] = map[var] " " key_of(line)
            next
        }
        # A name a secret is mounted under. Which workload mounts which is a
        # merge of maps this does not evaluate, so a mounted secret is optional
        # everywhere rather than required somewhere.
        if (FILENAME ~ /secrets\.tf$/) {
            if (line ~ /^[A-Z][A-Z0-9_]*[ \t]*=[ \t]*google_secret_manager_secret\./)
                secret[key_of(line)] = 1
            if (line ~ /: *"[A-Z][A-Z0-9_]*"[ \t]*=>/) {
                name = line; sub(/^.*: *"/, "", name); sub(/".*$/, "", name)
                secret[name] = 1
            }
        }
        next
    }

    {
        opens = gsub(/\{/, "{", line)
        closes = gsub(/\}/, "}", line)
        before = depth
        depth += opens - closes
    }

    workload == "" {
        if (before != 0) next
        else if (line ~ /^resource "google_cloud_run_v2_service"/) kind = "service"
        else if (line ~ /^resource "google_cloud_run_v2_job"/) kind = "job"
        else next
        workload = "?"; next
    }

    # A resource names itself before it names anything else, so the first
    # `name =` under it is the workload and every later one is a variable.
    workload == "?" && line ~ /^name[ \t]*=/ { workload = quoted(line); next }

    {
        if (depth == 0) { workload = ""; next }

        if (inenv) {
            if (line ~ /^name[ \t]*=/) envname = quoted(line)
            else if (line ~ /^value[ \t]*=/) {
                envvalue = rhs(line)
                if (envvalue ~ /^".*"$/) envvalue = substr(envvalue, 2, length(envvalue) - 2)
                else envvalue = "?"
            }
            else if (line ~ /^value_source[ \t]*\{/) envsecret = 1
            if (depth < inenv) {
                say(envsecret ? "optional" : "required", envname, envvalue)
                inenv = 0
            }
            next
        }

        if (indyn) {
            if (line ~ /^for_each[ \t]*=/) foreach = rhs(line)
            else if (line ~ /^name[ \t]*=/) dynname = quoted(line)
            if (depth < indyn) {
                indyn = 0
                if (foreach ~ /^var\.[a-z_]+$/ && map[substr(foreach, 5)] != "") {
                    n = split(map[substr(foreach, 5)], keys, " ")
                    for (i = 1; i <= n; i++) say("required", keys[i], "?")
                } else if (foreach ~ /^local\.mounts\.[a-z]+\.secrets$/) {
                    for (name in secret) say("optional", name, "?")
                } else if (dynname != "") {
                    say("optional", dynname, "?")
                } else {
                    print "! " workload " " foreach
                }
            }
            next
        }

        if (line ~ /^env[ \t]*\{/) { inenv = depth; envname = ""; envvalue = "?"; envsecret = 0 }
        else if (line ~ /^dynamic "env"[ \t]*\{/) { indyn = depth; foreach = ""; dynname = "" }
    }
    ' pass=1 "$TERRAFORM"/*.tf pass=2 "$TERRAFORM"/*.tf | sort -u
}

# What a workload actually carries, as `<NAME> <value|?>`. A secret's value is
# not read: the question is whether the mount is there, and nothing here has
# any business holding the value.
live() {
    local kind=$1 at
    case "$kind" in
        service) at='.spec.template.spec.containers[0]' ;;
        job)     at='.spec.template.spec.template.spec.containers[0]' ;;
        *)       fail "drift: $kind is not a kind of workload"; return 2 ;;
    esac
    gcloud run "${kind}s" describe "$2" --project "$PROJECT" --region "$REGION" --format=json </dev/null |
        jq -r "$at"' | .env[]? | "\(.name) \(if has("value") then .value else "?" end)"' 2>/dev/null
}

# Every workload deploy/terraform declares, or the ones named.
workloads() {
    local all found name
    all="$(declared | awk '$1 != "!" { print $1, $2 }' | sort -u)"
    [ "$#" -eq 0 ] && { printf '%s\n' "$all"; return 0; }
    for name in "$@"; do
        found="$(printf '%s\n' "$all" | awk -v n="$name" '$1 == n { print; f = 1 } END { exit !f }')" || {
            fail "drift: deploy/terraform declares no workload called $name"
            return 1
        }
        printf '%s\n' "$found"
    done
}

# One workload, both directions.
compare() {
    local name=$1 kind=$2 declaration=$3 running bad=0 need var value has
    running="$(live "$kind" "$name")"
    if [ -z "$running" ]; then
        said "$name could not be read, so nothing here says it is right or wrong"
        return 3
    fi

    while read -r need var value; do
        has="$(printf '%s\n' "$running" |
            awk -v v="$var" '$1 == v { $1 = ""; sub(/^ /, ""); print; f = 1 } END { exit !f }')" || {
            [ "$need" = required ] && {
                said "$name has no $var, which deploy/terraform declares"
                bad=1
            }
            continue
        }
        [ "$value" = "?" ] || [ "$has" = "?" ] || [ "$has" = "$value" ] || {
            said "$name has $var=$has, and deploy/terraform declares $value"
            bad=1
        }
    done < <(printf '%s\n' "$declaration" |
        awk -v n="$name" '$1 == n { v = $5; for (i = 6; i <= NF; i++) v = v " " $i; print $3, $4, v }')

    # The other direction, which is what a hand-patched service leaves behind:
    # an apply would take it away, so it is drift and not a spare.
    while read -r var value; do
        [ -n "$var" ] || continue
        printf '%s\n' "$declaration" |
            awk -v n="$name" -v v="$var" '$1 == n && $4 == v { f = 1 } END { exit !f }' && continue
        said "$name has $var, which deploy/terraform does not declare"
        bad=1
    done <<< "$running"

    [ "$bad" -eq 0 ] && said "$name carries what deploy/terraform declares"
    return "$bad"
}

check() {
    local declaration unread list name kind status unknown=0 bad=0
    declaration="$(declared)"

    unread="$(printf '%s\n' "$declaration" |
        awk '$1 == "!" { v = $3; for (i = 4; i <= NF; i++) v = v " " $i; print "  " $2 ": " v }')"
    if [ -n "$unread" ]; then
        said "deploy/terraform has a for_each this check cannot read, so it can say nothing:"
        printf '%s\n' "$unread"
        return 3
    fi

    list="$(workloads "$@")" || return 3
    if [ -z "$list" ]; then
        said "deploy/terraform declares no Cloud Run workload, so this checked nothing"
        return 3
    fi

    while read -r name kind; do
        [ -n "$name" ] || continue
        compare "$name" "$kind" "$declaration"
        case $? in
            0) ;;
            1) bad=1 ;;
            *) unknown=1 ;;
        esac
    done <<< "$list"

    if [ "$bad" -eq 1 ]; then
        said "the deployment has drifted from deploy/terraform"
        echo "Apply deploy/terraform, or change it to say what the deployment should be." >&2
        return 1
    fi
    if [ "$unknown" -eq 1 ]; then
        said "the deployment could not be checked; this is not a finding of drift"
        echo "Fix what stopped the reading (project, region, permissions) and run it again." >&2
        return 3
    fi
    said "every workload carries what deploy/terraform declares"
}

case "${1:-}" in
    declared) shift; declared ;;
    check)
        shift
        [ -n "${PROJECT:-}" ] || { fail "drift: PROJECT is unset."; exit 3; }
        [ -n "${REGION:-}" ] || { fail "drift: REGION is unset."; exit 3; }
        check "$@"
        ;;
    *)
        echo "usage: tools/nzfleet/release/drift.sh declared|check [workload...]" >&2
        exit 2
        ;;
esac
