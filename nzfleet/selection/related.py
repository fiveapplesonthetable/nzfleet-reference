"""The unit tests that can notice a change, for the pre-push hook: console modules and fleet modules."""

from __future__ import annotations

import re

EVERYTHING = "*"

_FLEET_MODULE = re.compile(r"^tools/nzfleet/nzfleet/([A-Za-z0-9_]+)(?:\.py|/.+\.py)$")
_FLEET_TEST = re.compile(r"^tools/nzfleet/tests/(test_[A-Za-z0-9_]+)\.py$")
_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s.*$", re.M)


def fleet(paths: list, tests: dict) -> list:
    """The `tests.<module>` names to run for a change under tools/nzfleet, or `*` when a path is not a module.

    `tests` maps each test file path to its text. A test is related to a module when one of its import lines
    names the module; a module no test imports is untested here and selects nothing.
    """
    chosen = set()
    for path in paths:
        if not path.startswith("tools/nzfleet/"):
            continue
        if test := _FLEET_TEST.match(path):
            chosen.add(test[1])
        elif module := _FLEET_MODULE.match(path):
            chosen |= {_name(t) for t, text in tests.items() if _imports(text, module[1])}
        elif not path.startswith("tools/nzfleet/tests/"):
            return [EVERYTHING]
        else:
            chosen |= {_name(t) for t in tests}
    return sorted(f"tests.{name}" for name in chosen)


def _name(test_path: str) -> str:
    return test_path.rsplit("/", 1)[1].removesuffix(".py")


def _imports(text: str, module: str) -> bool:
    return any(re.search(rf"\b{re.escape(module)}\b", line) for line in _IMPORT_LINE.findall(text))


def console(paths: list, modules: dict, tests: set) -> list:
    """The console unit test files whose imports reach a change, or `*` when the console's configuration changed."""
    # Imported here because ci's select job copies only this package, and no selection other than this one needs the plan.
    from ..devloop import plan

    chosen = plan.console_plan(paths, modules, tests)
    if chosen["mode"] == "all":
        return [EVERYTHING]
    return [path.removeprefix(plan.ADMIN) for path in chosen.get("tests", [])]
