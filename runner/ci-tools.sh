#!/bin/sh
#
# tools/nzfleet/runner/ci-tools.sh <set>
#
# Makes the tools a job needs beyond its pinned image. The first job to ask on a runner installs them and keeps
# the files they added under /warm/tools; every later job unpacks that archive in a second instead of running apt.
# The archive is named for the image it was made on, so a new image digest installs once more. Without a /warm
# volume, or with a read-only one, the set is installed as it always was.
#
#   rust-checks   clippy, rustfmt, the PGDG Postgres 16 client, jq
#   rust-client   clippy, rustfmt, the distribution Postgres client
#   pg-client     the distribution Postgres client
#   xz            xz-utils, which setup-node unpacks its download with
#   shell-checks  curl, jq, python3-yaml, unzip

set -eu

set_name=${1:?usage: ci-tools.sh <set>}
store=${NZELA_TOOLS_DIR:-/warm/tools}

install_set() {
    case "$set_name" in
    rust-checks)
        rustup component add clippy rustfmt
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends postgresql-common
        /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
        apt-get install -y -qq --no-install-recommends postgresql-client-16 jq
        ;;
    rust-client)
        rustup component add clippy rustfmt
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends postgresql-client
        ;;
    pg-client)
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends postgresql-client
        ;;
    xz)
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends xz-utils
        ;;
    shell-checks)
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends curl jq python3-yaml unzip
        ;;
    *)
        echo "ci-tools.sh: no tool set named $set_name" >&2
        exit 2
        ;;
    esac
}

# The roots the installs write to. dpkg's status file is rewritten rather than added, so it travels explicitly.
listing() {
    find /usr /etc /var/lib/dpkg/info /var/lib/dpkg/alternatives /opt \
        -xdev \( -type f -o -type l \) 2>/dev/null | sort
    echo /var/lib/dpkg/status
}

key=$( (cat "$0"; cat /etc/os-release; rustc --version 2>/dev/null; uname -m) | sha256sum | cut -c1-16)
archive="$store/$set_name-$key.tar.gz"

if [ -f "$archive" ] && tar -xzf "$archive" -C /; then
    exit 0
fi

if [ ! -d "$store" ] && ! mkdir -p "$store" 2>/dev/null || [ ! -w "$store" ]; then
    install_set
    exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
listing >"$work/before"
install_set
listing >"$work/after"
comm -13 "$work/before" "$work/after" >"$work/added"
echo /var/lib/dpkg/status >>"$work/added"
sort -u "$work/added" -o "$work/added"
staged=$(mktemp "$store/.$set_name.XXXXXX")
if tar -czf "$staged" -C / -T "$work/added" 2>/dev/null; then
    mv "$staged" "$archive"
    for old in "$store/$set_name"-*.tar.gz; do [ "$old" = "$archive" ] || rm -f "$old"; done
else
    rm -f "$staged"
fi
