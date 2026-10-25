#!/bin/sh
# Install the exact Nextest release whose archive format CI uses.
set -eu
version=0.9.127
sha256=0a9d356170528ba92a65fd0157ec0445a67d1f7c2c5726085656e55ba8a1b976
binary_sha256=ec48c1562f355d84c49796a342366c0ef21fb33320a982e8d1907c41319649ae
dir=${1:?usage: install-nextest.sh <directory>}
bin=$dir/cargo-nextest
if [ ! -x "$bin" ] || ! echo "$binary_sha256  $bin" | sha256sum -c - >/dev/null 2>&1; then
    work=$(mktemp -d "${TMPDIR:-/tmp}/nextest.XXXXXX")
    trap 'rm -rf "$work"' EXIT
    name=cargo-nextest-$version-x86_64-unknown-linux-gnu.tar.gz
    curl -fsSL --retry 2 -m 120 -o "$work/$name" \
        "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-$version/$name"
    echo "$sha256  $work/$name" | sha256sum -c - >&2
    tar -xzf "$work/$name" -C "$work" cargo-nextest
    echo "$binary_sha256  $work/cargo-nextest" | sha256sum -c - >&2
    mkdir -p "$dir"
    staged=$(mktemp "$dir/.cargo-nextest.XXXXXX")
    mv "$work/cargo-nextest" "$staged"
    chmod 755 "$staged"
    mv "$staged" "$bin"
fi
printf '%s\n' "$bin"
