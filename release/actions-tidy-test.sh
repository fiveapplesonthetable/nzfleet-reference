#!/bin/bash
# What actions-tidy.sh deletes, against a stand-in `gh` serving caches and
# artifacts of several ages and names.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/bin"
failed=0

cat > "$work/fixture-caches" <<'JSON'
{"actions_caches":[{"id":11,"last_accessed_at":"2000-01-01T00:00:00Z"},{"id":12,"last_accessed_at":"2999-01-01T00:00:00Z"}]}
JSON
ago() { date -u -d "$1 ago" +%Y-%m-%dT%H:%M:%SZ; }
artifact() { printf '{"id":%s,"name":"%s","created_at":"%s","expired":%s}' "$1" "$2" "$(ago "$3")" "${4:-false}"; }
# A fourth archive settled two hours goes; a fourth an hour old, a run still
# reading it, stays; and a pull request's image waits for age alone.
{
    printf '{"artifacts":['
    artifact 21 old "5 days"; printf ,
    artifact 22 old "1 hour"; printf ,
    artifact 23 old "6 days" true; printf ,
    artifact 31 archive "3 hours"; printf ,
    artifact 32 archive "4 hours"; printf ,
    artifact 33 archive "5 hours"; printf ,
    artifact 34 archive "6 hours"; printf ,
    artifact 41 running "10 minutes"; printf ,
    artifact 42 running "20 minutes"; printf ,
    artifact 43 running "30 minutes"; printf ,
    artifact 44 running "40 minutes"; printf ,
    artifact 51 release-image "3 hours"; printf ,
    artifact 52 release-image "4 hours"; printf ,
    artifact 53 release-image "5 hours"; printf ,
    artifact 54 release-image "6 hours"
    printf ']}\n'
} > "$work/fixture-artifacts"
cat > "$work/bin/gh" <<'SH'
#!/bin/bash
args=("$@")
for i in "${!args[@]}"; do
    case "${args[$i]}" in
        DELETE) echo "${args[$((i + 1))]}" >> "$WORK/deleted"; exit 0 ;;
        --jq) filter="${args[$((i + 1))]}" ;;
        repos/*/actions/caches*) fixture="$WORK/fixture-caches" ;;
        repos/*/actions/artifacts*) fixture="$WORK/fixture-artifacts" ;;
    esac
done
[ "${GH_FAIL_LISTING:-}" != 1 ] || exit 42
jq -r "$filter" "$fixture"
SH
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH" WORK="$work" REPO=o/r

out="$(scripts_dir="$here" "$here/actions-tidy.sh" 3 2>&1)" || { echo "  tidy failed: $out" >&2; failed=1; }
deleted="$(sort "$work/deleted" 2>/dev/null | tr '\n' ' ')"
[ "$deleted" = "repos/o/r/actions/artifacts/21 repos/o/r/actions/artifacts/34 repos/o/r/actions/caches/11 " ] || {
    echo "  deleted: ${deleted:-nothing}; wanted the old cache, the old artifact and the fourth settled archive" >&2
    failed=1
}

rm -f "$work/deleted"
DRY_RUN=1 "$here/actions-tidy.sh" 3 >/dev/null 2>&1
[ ! -e "$work/deleted" ] || { echo "  a dry run deleted something" >&2; failed=1; }

if GH_FAIL_LISTING=1 "$here/actions-tidy.sh" 3 >"$work/listing-error" 2>&1; then
    echo "  a failed listing was reported as a successful tidy" >&2
    failed=1
fi

# The workflow runs this script from a sparse checkout; a move it does not follow
# left storage untidied for two days with every run red.
workflow="$here/../../../.github/workflows/actions-tidy.yaml"
dir="$(sed -n 's/^ *sparse-checkout: *//p' "$workflow")"
script="$(sed -n 's/^ *run: *\(tools[^ ]*actions-tidy\.sh\).*/\1/p' "$workflow")"
[ -n "$dir" ] && [ -n "$script" ] && [ "${script#"$dir"/}" != "$script" ] || {
    echo "  the workflow checks out '${dir:-nothing}' but runs '${script:-nothing}'" >&2
    failed=1
}

[ "$failed" -eq 0 ] && echo "actions-tidy: all cases passed"
exit "$failed"
