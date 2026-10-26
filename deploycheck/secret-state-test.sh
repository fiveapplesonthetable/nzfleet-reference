#!/bin/bash
# Terraform keeps no credential in state. A payload attribute that is not
# write-only, or a generated password that is not ephemeral, would put one
# there; so would a variable that carries one in.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$root"
bad=0

if grep -nE '^[[:space:]]*(secret_data|password)[[:space:]]*=' deploy/terraform/*.tf; then
    echo "Use secret_data_wo and password_wo: the plain forms are stored in state." >&2
    bad=1
fi
if grep -nE '^resource "(random_password|random_string|random_id|random_bytes|google_secret_manager_secret_version_data|tls_private_key|google_service_account_key)"' deploy/terraform/*.tf; then
    echo "Generate a credential with an ephemeral resource, not a resource." >&2
    bad=1
fi
if grep -nE 'data "google_secret_manager_secret_version' deploy/terraform/*.tf; then
    echo "A data source copies the secret into state; mount a version by number." >&2
    bad=1
fi
if awk '/^variable "/ { name = $2; gsub(/"/, "", name) }
        /sensitive[[:space:]]*=[[:space:]]*true/ { print FILENAME ": " name; found = 1 }
        END { exit !found }' deploy/terraform/variables.tf; then
    echo "A sensitive variable is a credential in state; add it as a secret version." >&2
    bad=1
fi

[ "$bad" -eq 0 ] && echo "no credential is stored in Terraform state"
exit "$bad"
