"""The tests a change names, for repeating and for running only what it affects."""

from __future__ import annotations

import os
import re

MONEY_TESTS = (
    "nzela-money money", "nzela-money pricing", "nzela-ledger ledger",
    "nzela-lifecycle transfer", "nzela-disbursement payout", "nzela-disbursement refund",
    "nzela-transfer collection", "nzela-ledger movements", "nzela-ledger reservation",
    "nzela-disbursement remedies", "nzela-ledger float_actions", "nzela-money rate_band",
    "nzela-rates rate_actions", "nzela-rates terms_actions", "nzela-lifecycle step",
    "nzela-lifecycle outbound", "nzela --test properties",
)
EVERYTHING = "*"

_RUST_TEST = re.compile(r"^crates/([^/]+)/tests/([^/]+)\.rs$")
_RUST_ENTRY = re.compile(r"^crates/[^/]+/src/(lib|main|bin/.*)\.rs$")
_RUST_MOD = re.compile(r"^crates/([^/]+)/src/(.+)/mod\.rs$")
_RUST_SRC = re.compile(r"^crates/([^/]+)/src/(.+)\.rs$")
_ANDROID_TEST = re.compile(r"^frontend/android/([a-z]+/)+src/(test|sharedTest)/(java|kotlin)/(.+)/[^/]+Test\.kt$")
_PACKAGE = re.compile(r"^package[ \t]+([A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*)[ \t]*$", re.M)
_CONSOLE_SPEC = re.compile(r"^frontend/admin/e2e/[^/]+\.spec\.ts$")


class NoSuchArea(ValueError):
    """The area has no tests to select."""


def _unique(items) -> list:
    return list(dict.fromkeys(i for i in items if i))


def repeated(area: str, paths: list) -> list:
    """The money tests to repeat for a change: all of them when a crate file was deleted, else those the paths name."""
    if area != "rust":
        raise NoSuchArea(f"no repeated tests for area: {area}")
    if any(p.startswith("crates/") and not os.path.isfile(p) for p in paths):
        return list(MONEY_TESTS)
    out = []
    for path in paths:
        if match := _RUST_TEST.match(path):
            out.append(f"{match[1]} --test {match[2]}")
        elif _RUST_ENTRY.match(path):
            continue
        elif match := _RUST_MOD.match(path) or _RUST_SRC.match(path):
            out.append(f"{match[1]} {match[2].replace('/', '::')}")
    return _unique(out)


def _android_package(path: str) -> str | None:
    match = _ANDROID_TEST.match(path)
    if not match or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        declared = _PACKAGE.search(handle.read())
    expected = match[4].replace("/", ".")
    return f"{expected}.*" if declared and declared[1] == expected else None


def affected(area: str, paths: list) -> list:
    """The standalone targets for `area` when every changed input of the area maps to one; `["*"]` when any does not."""
    selected = []
    for path in paths:
        if area == "console":
            if not path.startswith("frontend/admin/"):
                continue
            if not os.path.isfile(path) or not _CONSOLE_SPEC.match(path):
                return [EVERYTHING]
            selected.append(path.removeprefix("frontend/admin/"))
        elif area == "android":
            if not path.startswith("frontend/android/"):
                continue
            package = _android_package(path)
            if package is None:
                return [EVERYTHING]
            selected.append(package)
        elif area == "rust":
            if not (path.startswith(("crates/", "fuzz/")) or path in ("Cargo.toml", "Cargo.lock")):
                continue
            match = _RUST_TEST.match(path)
            if not os.path.isfile(path) or not match:
                return [EVERYTHING]
            selected.append(f"{match[1]} --test {match[2]}")
        else:
            raise NoSuchArea(f"no such area: {area}")
    if area not in ("console", "android", "rust"):
        raise NoSuchArea(f"no such area: {area}")
    return _unique(selected)
