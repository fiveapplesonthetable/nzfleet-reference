"""`nzfleet-guard`: the rules the hook enforces about how work is done; standard library plus yaml."""

from __future__ import annotations

import os
import subprocess
import sys

from . import book_map, limits

USAGE = "usage: nzfleet-guard limits | book [dead]  (limits reads NZELA_BASE; book reads changed paths on stdin)"


def main(argv: list) -> int:
    """Dispatch `limits` and `book`."""
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or "."
    if argv == ["limits"]:
        return limits.main(os.environ.get("NZELA_BASE", ""))
    if argv == ["book", "dead"]:
        found = book_map.dead(root)
        print("\n".join(found))
        return 1 if found else 0
    if argv == ["book"]:
        print("\n".join(book_map.remind(root, [p for p in sys.stdin.read().splitlines() if p])))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
