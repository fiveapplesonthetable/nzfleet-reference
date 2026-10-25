#!/bin/sh
#
# tools/nzfleet/runner/install-sccache.sh <directory>
#
# Puts sccache in <directory> unless it is already there, and prints its path. The release is pinned by version and
# digest, so a compiler cache is not a way for whatever the download is served as to run in a build. Where the
# download fails nothing is printed and the build goes on without a cache.

set -eu

version=0.18.0
sha256=45f1447fbe231e3037bde351ef70677dd212216c8d62ae7ca409fecc4d6acc89
dir=${1:?usage: install-sccache.sh <directory>}
bin=$dir/sccache

if [ ! -x "$bin" ]; then
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    name=sccache-v$version-x86_64-unknown-linux-musl
    curl -fsSL --retry 2 -m 120 -o "$work/sccache.tgz" \
        "https://github.com/mozilla/sccache/releases/download/v$version/$name.tar.gz" || exit 0
    echo "$sha256  $work/sccache.tgz" | sha256sum -c - >/dev/null 2>&1 || exit 0
    tar -xzf "$work/sccache.tgz" -C "$work" "$name/sccache"
    mkdir -p "$dir"
    # Jobs in separate containers share this directory and can share a pid, so
    # the staging name comes from mktemp rather than $$.
    staged=$(mktemp "$dir/.sccache.XXXXXX")
    mv "$work/$name/sccache" "$staged" && chmod 755 "$staged" && mv "$staged" "$bin"
fi
echo "$bin"
