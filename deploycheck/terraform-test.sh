#!/bin/bash
# Terraform's own tests, and the root module still validating with them in it.
# Uses the terraform on the path or verifies the pinned release before caching it.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
version=1.11.4
cache_root="${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/nzela/terraform"

terraform_in() {
    local dir=$1
    shift
    local terraform_path architecture archive checksum_file checksum_line scratch
    if terraform_path="$(command -v terraform)"; then
        (cd "$dir" && "$terraform_path" "$@")
        return
    fi

    case "$(uname -m)" in
        x86_64) architecture=amd64 ;;
        aarch64) architecture=arm64 ;;
        *) echo "terraform $version: unsupported architecture $(uname -m)" >&2; return 1 ;;
    esac

    terraform_path="$cache_root/$version/$architecture/terraform"
    if [ ! -x "$terraform_path" ] || ! "$terraform_path" version 2>/dev/null | grep -q "Terraform v$version"; then
        mkdir -p "$(dirname "$terraform_path")" || return 1
        scratch="$(mktemp -d "$(dirname "$terraform_path")/.download.XXXXXX")" || return 1
        archive="terraform_${version}_linux_${architecture}.zip"
        checksum_file="$scratch/SHA256SUMS"
        if ! curl --fail --location --silent --show-error \
            "https://releases.hashicorp.com/terraform/${version}/${archive}" -o "$scratch/$archive"; then
            echo "terraform $version: release download failed" >&2
            rm -rf "$scratch"
            return 1
        fi
        if ! curl --fail --location --silent --show-error \
            "https://releases.hashicorp.com/terraform/${version}/terraform_${version}_SHA256SUMS" -o "$checksum_file"; then
            echo "terraform $version: SHA256SUMS download failed" >&2
            rm -rf "$scratch"
            return 1
        fi
        checksum_line="$(awk -v archive="$archive" '$2 == archive { print $1 "  " $2 }' "$checksum_file")"
        if [ -z "$checksum_line" ] || [ "$(printf '%s\n' "$checksum_line" | wc -l)" -ne 1 ] \
            || ! (cd "$scratch" && printf '%s\n' "$checksum_line" | sha256sum --check --status); then
            echo "terraform $version: release checksum verification failed" >&2
            rm -rf "$scratch"
            return 1
        fi
        if ! unzip -q "$scratch/$archive" -d "$scratch" || [ ! -f "$scratch/terraform" ]; then
            echo "terraform $version: release archive is invalid" >&2
            rm -rf "$scratch"
            return 1
        fi
        chmod 0755 "$scratch/terraform" || { rm -rf "$scratch"; return 1; }
        mv -f "$scratch/terraform" "$terraform_path" || { rm -rf "$scratch"; return 1; }
        rm -rf "$scratch"
    fi
    (cd "$dir" && "$terraform_path" "$@")
}

bad=0
for dir in "$root/deploy/terraform/monitoring"; do
    terraform_in "$dir" init -backend=false -input=false >/dev/null || bad=1
    terraform_in "$dir" test || bad=1
done
terraform_in "$root/deploy/terraform" init -backend=false -input=false >/dev/null || bad=1
terraform_in "$root/deploy/terraform" validate || bad=1
exit "$bad"
