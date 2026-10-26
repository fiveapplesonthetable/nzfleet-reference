#!/bin/bash
#
# The book in docs/book, as one PDF.
#
#   tools/nzfleet/book/book.sh [OUT.pdf]        default /tmp/nzela-book.pdf, or $NZELA_BOOK_OUT
#   BOOK_CHECK=1 BOOK_ONLY=06,09 tools/nzfleet/book/book.sh
#   BOOK_STATS=1 prints each diagram's printed height and text size
#
# BOOK_CHECK draws the diagrams and checks every image and link without laying
# out pages; BOOK_ONLY names the chapters by number. Both are for a chapter
# being written.
#
# The contents group the chapters into parts: a chapter whose front matter has
# `part` and `partSummary` opens a part with its own page, and `appendix: true`
# labels a chapter A.
#
# The chapters are Markdown with Mermaid diagrams, so they render on GitHub and
# diff as text. This turns them into one page, draws the diagrams with
# mermaid.js and prints it with the Chromium in the Playwright image the
# console's screenshots use. The image tag comes from the console's lock file,
# so a browser a version off its driver is not possible here either.
#
# The PDF is never committed. The dependencies are installed once into a cache
# outside the tree from docs/book/package-lock.json.

set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
out="${1:-${NZELA_BOOK_OUT:-/tmp/nzela-book.pdf}}"
cache="${NZELA_BOOK_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/nzela-book}"

version="$(awk '/"node_modules\/playwright": \{/ { getline; gsub(/[^0-9.]/, "", $0); print; exit }' \
    "$root/frontend/admin/package-lock.json")"
[ -n "$version" ] || { echo "book: no playwright version in frontend/admin/package-lock.json" >&2; exit 1; }
want="$(awk '/"playwright-core":/ { gsub(/[^0-9.]/, "", $2); print $2; exit }' "$root/docs/book/package.json")"
[ "$version" = "$want" ] || {
    echo "book: docs/book wants playwright-core $want and the console's lock file says $version" >&2
    exit 1
}

docker info >/dev/null 2>&1 || { echo "book: a docker daemon is needed for the pinned browser" >&2; exit 1; }

mkdir -p "$cache" "$(dirname -- "$out")"
outdir="$(cd -- "$(dirname -- "$out")" && pwd)"

commit="$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)"

exec docker run --rm --ipc=host \
    --user "$(id -u):$(id -g)" \
    --volume "$root:/repo:ro" \
    --volume "$cache:/cache" \
    --volume "$outdir:/out" \
    --workdir /cache \
    --env HOME=/tmp \
    --env BOOK_REPO=/repo \
    --env BOOK_COMMIT="$commit" \
    --env BOOK_ONLY="${BOOK_ONLY:-}" \
    --env BOOK_CHECK="${BOOK_CHECK:-}" \
    --env BOOK_DRAFT="${BOOK_DRAFT:-}" \
    --env BOOK_STATS="${BOOK_STATS:-}" \
    --env BOOK_DATE="$(date -u +%-d\ %B\ %Y)" \
    "mcr.microsoft.com/playwright:v$version-noble" \
    sh -c '
        if ! cmp -s /repo/docs/book/package-lock.json /cache/package-lock.json; then
            rm -rf /cache/node_modules
            cp /repo/docs/book/package.json /repo/docs/book/package-lock.json /cache/
            npm ci --no-audit --no-fund --loglevel=error
        fi
        NODE_PATH=/cache/node_modules node /repo/docs/book/build.cjs "/out/'"$(basename -- "$out")"'"
    '
