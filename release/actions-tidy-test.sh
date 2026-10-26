#!/bin/bash
# What actions-tidy.sh deletes, against a stand-in `gh` serving two caches and
# two artifacts, one of each long old.

set -uo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/bin"
failed=0

cat > "$work/fixture-caches" <<'JSON'
{"actions_caches":[{"id":11,"last_accessed_at":"2000-01-01T00:00:00Z"},{"id":12,"last_accessed_at":"2999-01-01T00:00:00Z"}]}
JSON
cat > "$work/fixture-artifacts" <<'JSON'
{"artifacts":[{"id":21,"created_at":"2000-01-01T00:00:00Z"},{"id":22,"created_at":"2999-01-01T00:00:00Z"}]}
JSON
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
[ "$deleted" = "repos/o/r/actions/artifacts/21 repos/o/r/actions/caches/11 " ] || {
    echo "  deleted: ${deleted:-nothing}; wanted only the two old ones" >&2
    failed=1
}

rm -f "$work/deleted"
DRY_RUN=1 "$here/actions-tidy.sh" 3 >/dev/null 2>&1
[ ! -e "$work/deleted" ] || { echo "  a dry run deleted something" >&2; failed=1; }

if GH_FAIL_LISTING=1 "$here/actions-tidy.sh" 3 >"$work/listing-error" 2>&1; then
    echo "  a failed listing was reported as a successful tidy" >&2
    failed=1
fi

[ "$failed" -eq 0 ] && echo "actions-tidy: all cases passed"
exit "$failed"
