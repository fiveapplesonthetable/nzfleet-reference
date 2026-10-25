"""Refuse a diff that loosens a timeout, a size budget or a retry count, or forces serial tests.

A red check is fixed at its cause; lengthening what it waits for only stops it saying so.
"""

from __future__ import annotations

import re
import subprocess
import sys

EXCLUDED = (":!tools/nzfleet", ":!docs", ":!*.md")
NUMBER = re.compile(r"^[0-9][0-9_]*( *\* *[0-9][0-9_]*)*")
KEY = re.compile(r"[A-Za-z_]*([Tt]imeout|TIMEOUT|[Bb]udget|BUDGET|[Rr]etries)[A-Za-z_]*[ ]*[:=(][ ]*")
SERIAL = re.compile(r"mode: *.serial.|test\.slow\(|fullyParallel: *false|--workers[= ]1([^0-9]|$)")
ADVICE = ("a check that is red is red for a reason: find it and fix it. Do not lengthen a timeout, raise a\n"
          "budget, add a retry or force serial mode; if the cause cannot be found, report that and stop.")


def _value(text: str):
    found = NUMBER.match(text)
    if not found:
        return None
    product = 1
    for part in found.group(0).split("*"):
        product *= int(part.replace(" ", "").replace("_", ""))
    return product


def _keys(line: str) -> dict:
    """Every limit-shaped name on the line with the number it is set to."""
    out = {}
    rest = line
    while True:
        found = KEY.search(rest)
        if not found:
            return out
        tail = rest[found.end():]
        value = _value(tail)
        if value is not None:
            out[re.sub(r"[ ]*[:=(][ ]*$", "", found.group(0))] = value
        rest = tail


def refusals(diff: str) -> list:
    """One line per loosened limit in a zero-context unified diff."""
    out, file, removed = [], "", {}
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            file, removed = raw[6:], {}
        elif raw.startswith("--- "):
            continue
        elif raw.startswith("-"):
            removed.update(_keys(raw[1:]))
        elif raw.startswith("+"):
            line = raw[1:]
            for name, value in _keys(line).items():
                if name in removed and value > removed[name]:
                    out.append(f"{file}: {name} raised from {removed[name]} to {value}")
            if SERIAL.search(line):
                out.append(f"{file}: {line}")
    return out


def main(base: str) -> int:
    """Check `HEAD` against the commit `base`, in the current repository."""
    if not base:
        print("no-loosened-limits: NZELA_BASE is required", file=sys.stderr)
        return 1
    resolved = subprocess.run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
                              capture_output=True, text=True)
    if resolved.returncode:
        print(f"no-loosened-limits: NZELA_BASE is not an available commit: {base}", file=sys.stderr)
        return 1
    diff = subprocess.run(["git", "diff", "-U0", "--no-color", f"{resolved.stdout.strip()}...HEAD", "--", ".", *EXCLUDED],
                          capture_output=True, text=True, check=True).stdout
    found = refusals(diff)
    if found:
        print("\n".join(found), file=sys.stderr)
        print(ADVICE, file=sys.stderr)
    return 1 if found else 0
