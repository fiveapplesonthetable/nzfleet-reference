#!/bin/bash
#
# shot-report.sh <area> <golden-file> <path>...
#
# What a run redrew under the given tracked paths. The new bytes stay in the
# tree; the old ones are read from HEAD, and both go into
# $SHOT_REPORT/<area>/ beside an index.html that shows them side by side.
# Exits 1 when a golden screenshot moved or is gone, unless the run is for
# review only. A golden entry with no screenshot always fails.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1

area="${1:?area}" golden="${2:?golden file}"
shift 2
tools/nzfleet/bin/nzfleet-select require shot-report "$golden" || exit 1
[ "$#" -gt 0 ] || { echo "shot-report: no screenshot paths were supplied" >&2; exit 1; }
out="${SHOT_REPORT:-$PWD/shot-report}/$area"
rm -rf "$out"

# The golden file sits beside the directory it governs: screenshots.golden holds names in screenshots/.
dir="${golden%.golden}"
strict=0
while IFS= read -r name; do
    [ -e "$dir/$name" ] && continue
    printf 'golden: %s names no screenshot in %s\n' "$name" "$dir" >&2
    strict=1
done < <(grep -v '^#' "$golden" | grep .)

if ! moved="$(git status --porcelain --untracked-files=all -- "$@")"; then
    echo "shot-report: git could not inspect changed screenshots" >&2
    exit 1
fi
if [ -z "$moved" ]; then
    exit "$strict"
fi

mkdir -p "$out/before" "$out/after"
: >"$out/changed.txt"
while IFS= read -r line; do
    code="${line:0:2}" path="${line:3}"
    case "$code" in
        '??'|' A'|'A '|'AM') state=new ;;
        ' D'|'D ') state=gone ;;
        *) state=changed ;;
    esac
    name="$(basename "$path")"
    held=""
    if [ "$path" = "$dir/$name" ] && grep -qxF -- "$name" "$golden"; then
        held=golden
        [ "${NZELA_SCREENSHOT_REVIEW_ONLY:-0}" = 1 ] || strict=1
    fi
    printf '%s\t%s\t%s\n' "$state" "$path" "$held" >>"$out/changed.txt"
    mkdir -p "$out/before/$(dirname "$path")" "$out/after/$(dirname "$path")"
    [ "$state" != new ] && git show "HEAD:$path" >"$out/before/$path" 2>/dev/null
    [ "$state" != gone ] && cp -- "$path" "$out/after/$path"
done <<<"$moved"

esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
{
    echo '<!doctype html><meta charset="utf-8"><title>Screenshots redrawn: '"$area"'</title>'
    echo '<style>body{font:14px/1.5 system-ui;margin:24px}.pair{display:flex;gap:12px;margin-bottom:24px}'
    echo 'figure{margin:0;flex:1}img{max-width:100%;border:1px solid #888}.golden{color:#b00}</style>'
    echo "<h1>Screenshots redrawn: $area</h1>"
    while IFS=$'\t' read -r state path held; do
        safe="$(printf '%s' "$path" | esc)"
        echo "<h2>$safe ($state${held:+, <span class=golden>golden</span>})</h2><div class=pair>"
        if [ -f "$out/before/$path" ] && [[ "$path" == *.png ]]; then
            echo "<figure><figcaption>before</figcaption><img src=\"before/$safe\"></figure>"
        fi
        if [ -f "$out/after/$path" ] && [[ "$path" == *.png ]]; then
            echo "<figure><figcaption>after</figcaption><img src=\"after/$safe\"></figure>"
        fi
        echo '</div>'
    done <"$out/changed.txt"
} >"$out/index.html"

total="$(wc -l <"$out/changed.txt")"
printf '%s: %s screenshot file(s) redrawn. Report: %s/index.html\n' "$area" "$total" "$out"
cut -f1,2,3 "$out/changed.txt" | sed 's/^/  /'
if [ "$strict" -ne 0 ]; then
    printf '\033[1;31m%s: a golden screenshot moved. Those are held exactly; `nzfleet shots fix <pr-or-main>` says whether the canvas, an intended change or drift moved it.\033[0m\n' "$area" >&2
    exit 1
fi
exit 0
