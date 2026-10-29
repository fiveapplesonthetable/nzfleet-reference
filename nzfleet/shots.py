"""Which screenshots a change may carry: those of the screens its own diff touches, and the golden set."""

from __future__ import annotations

import posixpath
import re

CONSOLE = "frontend/admin/"
ANDROID = "frontend/android/"
SHOT_DIRS = {"console": CONSOLE + "screenshots/", "android": ANDROID + "docs/screenshots/"}
REDRAWN = {"console": (SHOT_DIRS["console"], CONSOLE + "docs/comparison.html"),
           "android": (SHOT_DIRS["android"],)}
GENERATED = (CONSOLE + "screenshots/", CONSOLE + "docs/", ANDROID + "docs/")
# Screens whose shots are named for something else.
ALIASES = {"home": "dashboard", "settings": "preferences", "funding": "treasury", "provider": "treasury"}
SHARED_COMPOSE = {"components", "theme", "design", "nav", "system"}
SCREENSHOT_TESTS = "app/src/test/java/app/nzela/screenshot/"
MOVES_NO_PIXEL = re.compile(r"(\.md$|\.test\.[jt]sx?$|\.spec\.[jt]s$|/e2e/|/docs/|/androidTest/|\.golden$)")


def is_shot(path: str) -> bool:
    return path.endswith(".png") and any(path.startswith(d) for d in SHOT_DIRS.values())


def is_redrawn(path: str) -> bool:
    """Whether a check may regenerate this path."""
    return is_shot(path) or any(path == item for paths in REDRAWN.values() for item in paths if not item.endswith("/"))


def area_of(path: str) -> str:
    return "console" if path.startswith(CONSOLE) else "android"


def _first_word(name: str) -> str:
    word = re.match(r"[A-Z][a-z0-9]*|[a-z0-9]+", name)
    return word.group(0).lower() if word else ""


def _owner(path: str):
    """What a hand-written file owns: None when it moves no pixel, a set of words for one screen's shots,
    or True for every shot of its area."""
    if not path.startswith((CONSOLE, ANDROID)) or path.startswith(GENERATED) or MOVES_NO_PIXEL.search(path):
        return None
    stem = posixpath.splitext(posixpath.basename(path))[0]
    if path.startswith(CONSOLE):
        if posixpath.dirname(path) == CONSOLE + "src/screens":
            word = _first_word(stem)
            word = word[:-1] if word.endswith("s") else word
            return {word, ALIASES.get(word, word)}
        return True
    if SCREENSHOT_TESTS in path:
        return True
    if "/src/test/" in path:
        return None
    parts = path.split("/ui/", 1)
    if len(parts) == 2 and "/" in parts[1] and parts[1].split("/")[0] not in SHARED_COMPOSE:
        package = parts[1].split("/")[0]
        return {package, _first_word(stem)}
    return True


def _normal(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def stray(files: list, golden: set) -> list:
    """The screenshots among `files` that no hand-written file in `files` accounts for and the golden set does not hold."""
    owned = {"console": set(), "android": set()}
    every = set()
    for f in files:
        if is_shot(f):
            continue
        got = _owner(f)
        if got is True:
            every.add(area_of(f))
        elif got:
            owned[area_of(f)] |= got
    out = []
    for f in files:
        if not is_shot(f) or f in golden or area_of(f) in every:
            continue
        name = _normal(posixpath.basename(f))
        if not any(_normal(w) in name for w in owned[area_of(f)] if w):
            out.append(f)
    return out


def refusal(paths: list) -> str:
    listed = " ".join(paths)
    return (f"{len(paths)} screenshot(s) changed that no screen in this diff draws: {listed}. The pipeline redraws shots on "
            f"the merged tree; commit only the golden ones and those of the screens you changed. Drop them: "
            f"git checkout $NZ_BASE_SHA -- {listed} && git commit -m 'Leave the screenshots to the batch', "
            f"or run `nzfleet pr --shots-anyway` if the diff really moves them")


def stem(path: str) -> str:
    return re.sub(r"(-dark|-phone|-fr)+$", "", posixpath.splitext(posixpath.basename(path))[0])


def summary(moved: list) -> str:
    """How many shots moved, and for which screens."""
    if not moved:
        return "no shot moved"
    counts: dict = {}
    for path in moved:
        counts[stem(path)] = counts.get(stem(path), 0) + 1
    screens = ", ".join(f"{s} ({n})" if n > 1 else s for s, n in sorted(counts.items()))
    return f"{len(moved)} shot{'s' if len(moved) != 1 else ''} moved: {screens}"


GOLDEN_FILES = (CONSOLE + "screenshots.golden", ANDROID + "docs/screenshots.golden")


def golden_names(read) -> set:
    """The golden-listed shot paths; `read(path)` returns a golden file's text, or "" when there is none."""
    out = set()
    for golden in GOLDEN_FILES:
        for line in read(golden).splitlines():
            if line.strip() and not line.startswith("#"):
                out.add(golden[: -len(".golden")] + "/" + line.strip())
    return out
