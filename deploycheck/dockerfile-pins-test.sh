#!/bin/bash
# Every base image a Dockerfile in this repository builds from is named by
# digest, so a tag moved upstream cannot change what ships between a check and
# a deploy. A stage built FROM an earlier stage of the same file is exempt.

set -uo pipefail

root="$(cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$root"
dockerfiles="$(tools/nzfleet/bin/nzfleet-select tracked dockerfile-pins '*Dockerfile' '*/Dockerfile.*')" || exit 1

bad=0
while IFS= read -r file; do
    awk -v file="$file" '
        toupper($1) == "FROM" {
            i = 2
            while ($i ~ /^--/) i++
            image = $i
            if (!(image in stage) && image !~ /@sha256:[0-9a-f]{64}$/ && image != "scratch") {
                printf "%s:%d: %s is not pinned by digest\n", file, NR, image
                bad = 1
            }
            if (toupper($(i + 1)) == "AS") stage[$(i + 2)] = 1
        }
        END { exit bad }
    ' "$file" >&2 || bad=1
done <<< "$dockerfiles"

[ "$bad" -eq 0 ] || { echo "Pin the base with image:tag@sha256:<digest>." >&2; exit 1; }
echo "every base image is pinned by digest"
