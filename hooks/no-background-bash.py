#!/usr/bin/env python3
# An agent runs as `claude -p` and exits with its final message, so anything it left in the background dies with it.
import json
import sys

event = json.load(sys.stdin)
if (event.get("tool_input") or {}).get("run_in_background"):
    print("Denied: this session exits when you write your final message, and a background command dies with it. "
          "Run it in the foreground with a timeout, or start it with `nzfleet run` and wait on it with `nzfleet wait`.",
          file=sys.stderr)
    sys.exit(2)
