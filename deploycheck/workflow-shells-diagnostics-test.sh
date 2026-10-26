#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/broken.yaml" <<'YAML'
jobs:
  container-check:
    container: debian
    steps:
      - name: Read result
        run: cat <<< result
  host-check:
    steps:
      - name: Incomplete conditional
        run: if true; then
YAML

if "$here/workflow-shells-test.sh" "$work" >"$work/stdout" 2>"$work/stderr"; then
    echo 'FAIL: invalid sh and bash blocks passed' >&2
    exit 1
fi
grep -F "broken.yaml: job 'container-check', step 1 'Read result': sh syntax failed" "$work/stderr"
grep -F 'Syntax error:' "$work/stderr"
grep -F "broken.yaml: job 'host-check', step 1 'Incomplete conditional': bash syntax failed" "$work/stderr"
grep -F 'syntax error:' "$work/stderr"
[ ! -s "$work/stdout" ]

cat > "$work/broken.yaml" <<'YAML'
jobs:
  container-check:
    container: debian
    steps:
      - name: Read result
        shell: bash
        run: cat <<< result
      - run: printf '%s\n' result
YAML
"$here/workflow-shells-test.sh" "$work" >"$work/stdout" 2>"$work/stderr"
grep -Fx 'workflow-shells: 2 sh/bash run blocks passed' "$work/stdout"
[ ! -s "$work/stderr" ]
cat > "$work/broken.yaml" <<'YAML'
jobs:
  container-check:
    container: debian
    defaults:
      run:
        working-directory: frontend/admin
    steps:
      - name: Conditional
        run: '[[ -n "$X" ]] && true'
      - name: Wrong depth
        run: scripts/checks.sh console
      - name: Right depth
        run: ../../scripts/checks.sh console
      - name: Bash has it
        shell: bash
        run: '[[ -n "$X" ]] && true'
YAML
if "$here/workflow-shells-test.sh" "$work" >"$work/stdout" 2>"$work/stderr"; then
    echo 'FAIL: a bash-only test and a script path that does not resolve passed' >&2
    exit 1
fi
grep -F "step 1 'Conditional': sh step uses [[, which only bash has" "$work/stderr"
grep -F "step 2 'Wrong depth': scripts/checks.sh does not exist from the step's working directory" "$work/stderr"
[ "$(grep -c "step" "$work/stderr")" = 2 ]

echo 'invalid blocks name their workflow, job, step and shell error; valid blocks pass'
