#!/bin/bash
# No workflow names the repository it lives in; a rename or a second
# organisation would otherwise stop the image being published without a failure.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$here/../../.." || exit 1
workflow_files="$(tools/nzfleet/bin/nzfleet-select tracked workflow-repository '.github/workflows/*.yaml' '.github/workflows/*.yml')" || exit 1
bad=0

if hits="$(grep -nE "github\.repository *[!=]= *'" $workflow_files)"; then
    echo "  a workflow compares github.repository with a literal name:" >&2
    echo "$hits" >&2
    bad=1
fi

[ "$bad" -eq 0 ] && echo "no workflow hardcodes its repository"
exit "$bad"
