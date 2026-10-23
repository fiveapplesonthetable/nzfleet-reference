#!/bin/bash
# redraw-console.sh <checkout>: draws the console screenshots of a checkout from a
# preview server of its own. scripts/shots.sh captures whatever answers on its
# URL, and the default port is shared by every job on the machine, so a redraw
# that started nothing would capture someone else's console.
set -uo pipefail
cd "${1:?usage: redraw-console.sh <checkout>}/frontend/admin" || exit 1
npm ci --no-audit --no-fund >/dev/null 2>&1 || { echo "redraw: npm ci failed" >&2; exit 1; }
npm run build >/dev/null 2>&1 || { echo "redraw: the console does not build" >&2; exit 1; }
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
./node_modules/.bin/vite preview --host 127.0.0.1 --port "$port" --strictPort >/dev/null 2>&1 &
preview=$!
trap 'kill "$preview" 2>/dev/null' EXIT
until curl -fs "http://127.0.0.1:$port" >/dev/null; do
    kill -0 "$preview" 2>/dev/null || { echo "redraw: the preview stopped before it answered" >&2; exit 1; }
    sleep 0.2
done
SHOT_URL="http://127.0.0.1:$port" npm run shots
