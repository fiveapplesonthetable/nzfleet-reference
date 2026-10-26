#!/bin/bash
# No workload mounts a secret at `latest`. A mount names the exact version
# Terraform wrote, so rotating a value changes the template and rolls every
# mounting workload together.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$root"
terraform_files="$(tools/nzfleet/bin/nzfleet-select tracked secret-pins 'deploy/terraform/*.tf')" || exit 1

if grep -nE '^[[:space:]]*version[[:space:]]*=[[:space:]]*"latest"' $terraform_files; then
    echo "Mount a secret at local.secret_versions[...], not at latest." >&2
    exit 1
fi
echo "no secret is mounted at latest"
