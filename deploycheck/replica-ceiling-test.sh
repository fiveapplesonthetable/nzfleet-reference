#!/bin/bash
# tools/nzfleet/deploycheck/replica-ceiling.sh passes one instance and refuses more, whether the
# ceiling is a literal, a local, or something it cannot evaluate.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bad=0

# $1 is the line under test, $2 whether the check should pass.
case_of() {
    mkdir -p "$work/t" && rm -f "$work/t"/*.tf
    printf 'locals {\n  api_instances = 3\n  one = 1\n}\nscaling {\n%s\n}\n' "$1" > "$work/t/main.tf"
    "$here/replica-ceiling.sh" "$work/t" >/dev/null 2>&1
    got=$?
    [ "$got" = "$2" ] || { echo "  $1 returned $got, wanted $2" >&2; bad=1; }
}

case_of '  max_instance_count = 1' 0
case_of '  max_instance_count = local.one' 0
case_of '  max_instance_count = 2' 1
case_of '  max_instance_count = local.api_instances' 1
case_of '  max_instance_count = var.instances' 1
case_of '  min_instance_count = 0' 1

"$here/replica-ceiling.sh" >/dev/null 2>&1 || { echo "  deploy/terraform is not held to one instance" >&2; bad=1; }

[ "$bad" -eq 0 ] && echo "a ceiling above one instance is refused"
exit "$bad"
