#!/bin/sh
set -eu

here=$(cd "$(dirname "$0")" && pwd)
sh -n "$here/ci-tools.sh"

# A misspelt set must stop the job, not install nothing and pass.
status=0
NZELA_TOOLS_DIR=/nonexistent/tools sh "$here/ci-tools.sh" no-such-set 2>/dev/null || status=$?
[ "$status" -eq 2 ]
