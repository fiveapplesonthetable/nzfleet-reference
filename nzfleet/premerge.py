"""Main with named pull requests merged in order, and every check ci runs against the result.

ci reports on a merge that has already happened, and nothing else covers two branches that
are each green and are not green together.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


class Refused(Exception):
    """The run cannot continue; the message says why."""


def _git(*args: str, cwd=None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode:
        raise Refused(done.stderr.strip() or f"git {args[0]} failed")
    return done.stdout.strip()


def _fetch(remote: str, ref: str) -> str:
    """The commit `ref` is at on `remote` now."""
    _git("fetch", "--quiet", remote, ref)
    return _git("rev-parse", "FETCH_HEAD")


def _note(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m", flush=True)


def _remote() -> str:
    has_upstream = subprocess.run(["git", "remote", "get-url", "upstream"], capture_output=True).returncode == 0
    return "upstream" if has_upstream else "origin"


def _still_current(remote: str, base: str, numbers: list, heads: list) -> None:
    _note("premerge: confirming main and pull request heads are still current")
    if _fetch(remote, "main") != base:
        raise Refused("premerge: main moved while checks ran; run this against the new main")
    for number, head in zip(numbers, heads):
        if _fetch(remote, f"pull/{number}/head") != head:
            raise Refused(f"premerge: #{number} changed while checks ran; check its new head")


def run(numbers: list) -> int:
    """Check `numbers` merged onto main; the scratch tree stays when anything fails."""
    root = _git("rev-parse", "--show-toplevel")
    remote = _remote()
    _note(f"premerge: fetching main and {len(numbers)} pull request(s) from {remote}")
    base = _fetch(remote, "main")
    heads = []
    for number in numbers:
        try:
            heads.append(_fetch(remote, f"pull/{number}/head"))
        except Refused:
            raise Refused(f"premerge: no pull request {number} on {remote}") from None

    tree = os.path.join(tempfile.mkdtemp(prefix="nzela-premerge."), "tree")
    _git("worktree", "add", "--quiet", "--detach", tree, base, cwd=root)

    def keep(reason: str) -> int:
        print(reason, file=sys.stderr)
        print(f"\nthe tree is at {tree}. `git worktree remove --force {tree}` when done with it.")
        return 1

    env = {**os.environ, "NZELA_BASE": base}
    for number, head in zip(numbers, heads):
        _note(f"premerge: merging #{number}")
        if subprocess.run(["git", "merge", "--no-edit", "--quiet", head], cwd=tree).returncode:
            return keep(f"\033[1;31m#{number} conflicts. Nothing was checked.\033[0m")

    _note("premerge: npm ci")
    if subprocess.run(["npm", "ci", "--silent"], cwd=os.path.join(tree, "frontend/admin")).returncode:
        return keep("premerge: npm ci failed")
    lent = os.path.join(root, "frontend/android/local.properties")
    if not os.environ.get("ANDROID_HOME") and os.path.isfile(lent):
        with open(lent, "rb") as source, open(os.path.join(tree, "frontend/android/local.properties"), "wb") as out:
            out.write(source.read())

    if subprocess.run(["scripts/checks.sh", "all"], cwd=tree, env=env).returncode:
        return keep("premerge: checks failed")
    try:
        _still_current(remote, base, numbers, heads)
        if _fetch(remote, "main") != base:
            raise Refused("premerge: main moved while checks ran; run this against the new main")
    except Refused as error:
        return keep(str(error))

    _git("worktree", "remove", "--force", tree, cwd=root)
    print("\n\033[1;32m" + " ".join(f"#{n}" for n in numbers) + " on main: every check ci runs, passed.\033[0m")
    return 0


def main(argv: list) -> int:
    """`nzfleet-premerge 251 252`."""
    if not argv:
        print("premerge: give it pull request numbers. nzfleet-premerge 251 252", file=sys.stderr)
        return 1
    for number in argv:
        if not number.isdigit():
            print(f"premerge: '{number}' is not a pull request number", file=sys.stderr)
            return 1
    try:
        return run([int(n) for n in argv])
    except Refused as error:
        print(f"\033[1;31m{error}\033[0m", file=sys.stderr)
        return 1
