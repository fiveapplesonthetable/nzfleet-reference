"""The map from docs/book chapters to the code they describe.

Each chapter names its paths in a `covers:` front-matter list. An entry matches a file when it
matches the file's path or any directory above it.
"""

from __future__ import annotations

import os
import re
import subprocess

import yaml


def _regex(pattern: str):
    out, i = "", 0
    pattern = pattern.rstrip("/")
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(out)


def matches(pattern: str, path: str) -> bool:
    """Whether `pattern` names `path` or a directory above it."""
    rx = _regex(pattern)
    parts = path.split("/")
    return any(rx.fullmatch("/".join(parts[: n + 1])) for n in range(len(parts)))


def covers_of(text: str):
    """The chapter's `covers` list, or None when it has none."""
    found = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not found:
        return None
    try:
        meta = yaml.safe_load(found.group(1)) or {}
    except yaml.YAMLError:
        return None
    covers = meta.get("covers")
    return covers if isinstance(covers, list) and all(isinstance(c, str) for c in covers) else None


def _chapters(root: str) -> dict:
    """Chapter file name to its covers list (None when it has none)."""
    book = os.path.join(root, "docs/book")
    names = sorted(f for f in os.listdir(book) if re.fullmatch(r"\d\d-.+\.md", f)) if os.path.isdir(book) else []
    out = {}
    for name in names:
        with open(os.path.join(book, name), encoding="utf-8") as source:
            out[name] = covers_of(source.read())
    return out


def _present(pattern: str, names: set) -> bool:
    if not re.search(r"[*?]", pattern):
        return pattern.rstrip("/") in names
    rx = _regex(pattern)
    return any(rx.fullmatch(n) for n in names)


def dead(root: str) -> list:
    """A line for each chapter whose map is missing or points at no tracked file."""
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
    names = {"/".join(f.split("/")[: n + 1]) for f in tracked for n in range(f.count("/") + 1)}
    out = []
    for chapter, covers in _chapters(root).items():
        if not covers:
            out.append(f"docs/book/{chapter} has no covers list in its front matter")
            continue
        out += [f"docs/book/{chapter} covers {p}, which matches no tracked file"
                for p in covers if not _present(p, names)]
    return out


def remind(root: str, changed: list) -> list:
    """A line for each chapter whose paths the change touched and which the change did not."""
    out = []
    for chapter, covers in _chapters(root).items():
        if not covers or f"docs/book/{chapter}" in changed:
            continue
        hit = sorted({p for p in covers for c in changed if matches(p, c)})
        if hit:
            out.append(f"this change may need docs/book/{chapter} updated (it covers {', '.join(hit)})")
    return out
