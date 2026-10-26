"""The tests a change names, for repeating and for running only what it affects."""

from __future__ import annotations

import glob
import os
import re
import tomllib

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
_CRATE_FILE = re.compile(r"^crates/([^/]+)/(.+)$")
_CONSOLE_SPEC = re.compile(r"^frontend/admin/e2e/[^/]+\.spec\.ts$")


class NoSuchArea(ValueError):
    """The area has no tests to select."""


def _unique(items) -> list:
    return list(dict.fromkeys(i for i in items if i))


def _crate_dependents() -> dict:
    """Maps each workspace crate under crates/ to the crates that depend on it in any dependency table."""
    above = {}
    manifests = {}
    for manifest in sorted(glob.glob("crates/*/Cargo.toml")):
        with open(manifest, "rb") as handle:
            manifests[os.path.basename(os.path.dirname(manifest))] = tomllib.load(handle)
    for name in manifests:
        above[name] = set()
    for name, document in manifests.items():
        tables = [document]
        tables += [t for t in document.get("target", {}).values() if isinstance(t, dict)]
        for table in tables:
            for kind in ("dependencies", "dev-dependencies", "build-dependencies"):
                for dependency in table.get(kind, {}):
                    if dependency in above:
                        above[dependency].add(name)
    return above


def _reaching(names: set, above: dict) -> set:
    seen, pending = set(), list(names)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(above.get(name, ()))
    return seen


def _rust_affected(paths: list) -> list:
    """`crate` for each crate a change reaches, `crate --test binary` for a lone test file; `*` when a path names no crate."""
    above = _crate_dependents()
    source, own, binaries = set(), set(), {}
    for path in paths:
        if not path.startswith(("crates/", "fuzz/")) and path not in ("Cargo.toml", "Cargo.lock"):
            continue
        match = _CRATE_FILE.match(path)
        if not match or match[1] not in above or "/migrations/" in path:
            return [EVERYTHING]
        crate = match[1]
        if test := _RUST_TEST.match(path):
            binaries.setdefault(crate, set()).add(test[2])
        elif match[2].startswith("tests/"):
            own.add(crate)
        else:
            source.add(crate)
    # Nothing depends on a test file, so it reaches its own crate and no other.
    whole = _reaching(source, above) | own
    return [*sorted(whole), *(f"{crate} --test {binary}" for crate, found in sorted(binaries.items())
                               if crate not in whole for binary in sorted(found))]


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
    if area == "rust":
        return _rust_affected(paths)
    if area not in ("console", "android"):
        raise NoSuchArea(f"no such area: {area}")
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
    return _unique(selected)
