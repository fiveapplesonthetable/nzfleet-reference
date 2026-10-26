#!/bin/bash
#
# No Cloud Run workload may be allowed more than one instance.
#
#   tools/nzfleet/deploycheck/replica-ceiling.sh [terraform-dir]
#
# Admission is counted in each process's memory: the sign-in throttle, the
# per-address rate and every provider's request allowance. Two instances each
# spend the whole of them, so a ceiling above one multiplies every limit by the
# ceiling until a shared limiter exists (#281, #413). Whoever adds one removes
# this check in the same change.
#
# A ceiling this cannot read as a number is refused rather than passed.

set -uo pipefail

dir="${1:-$(cd -- "$(dirname -- "$0")/../../.." && pwd)/deploy/terraform}"

awk '
    function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t\r]+$/, "", s); return s }
    function rhs(s) { sub(/^[^=]*=[ \t]*/, "", s); return trim(s) }

    FNR == 1 { fname = FILENAME; sub(/.*\//, "", fname) }
    /^[ \t]*#/ { next }

    $0 ~ /^[ \t]+[a-z_]+[ \t]*=[ \t]*[0-9]+[ \t]*$/ {
        name = $1; value = rhs($0)
        if (name != "max_instance_count") local[name] = value
    }

    $0 ~ /^[ \t]*max_instance_count[ \t]*=/ {
        n++; where[n] = fname ":" FNR; expr[n] = rhs($0)
    }

    END {
        for (i = 1; i <= n; i++) {
            v = expr[i]
            if (v ~ /^local\.[a-z_]+$/) v = local[substr(v, 7)]
            if (v !~ /^[0-9]+$/) {
                printf "%s: max_instance_count = %s is not a number this can read\n", where[i], expr[i]
                bad = 1
            } else if (v + 0 > 1) {
                printf "%s: max_instance_count is %s, and admission limits are per instance\n", where[i], v
                bad = 1
            }
        }
        if (n == 0) { print "no max_instance_count found, so nothing was checked"; bad = 1 }
        exit bad
    }
' "$dir"/*.tf || {
    echo "Keep every workload at one instance until a shared limiter exists." >&2
    exit 1
}
echo "every workload is held to one instance"
