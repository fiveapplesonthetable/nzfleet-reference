"""The tests that can notice a change, for the pre-push hook: console modules, fleet modules and deploy checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

EVERYTHING = "*"


def source_extensions() -> tuple[str, ...]:
    """Read the file types shared with the browser coverage generator."""
    return tuple(json.loads((Path(__file__).resolve().parents[4] /
                             "frontend/admin/e2e/source-extensions.json").read_text(encoding="utf-8")))

_FLEET_MODULE = re.compile(r"^tools/nzfleet/nzfleet/([A-Za-z0-9_]+)(?:\.py|/.+\.py)$")
_FLEET_TEST = re.compile(r"^tools/nzfleet/tests/(test_[A-Za-z0-9_]+)\.py$")
_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s.*$", re.M)
_CONSOLE_IMPORT = re.compile(r"(?:from\s*|import\s+)['\"]([.@][^'\"]+)['\"]")


def fleet(paths: list, tests: dict, modules: dict | None = None) -> list:
    """The `tests.<module>` names to run for a change under tools/nzfleet.

    `tests` maps each test file path to its text and `modules` maps each module of the package to its text.
    A test is related to a module when one of its import lines names the module or a module that imports it.
    Any other file is related to the tests that name it; a file nothing names selects nothing.
    """
    modules = modules or {}
    chosen = set()
    for path in paths:
        if not path.startswith("tools/nzfleet/"):
            continue
        if test := _FLEET_TEST.match(path):
            chosen.add(test[1])
        elif module := _FLEET_MODULE.match(path):
            reaching = _importers(module[1], modules)
            chosen |= {_name(t) for t, text in tests.items() if any(_imports(text, m) for m in reaching)}
        elif path.startswith("tools/nzfleet/tests/"):
            chosen |= {_name(t) for t, text in tests.items() if _names_file(text, path.removeprefix("tools/nzfleet/tests/"))}
        else:
            chosen |= {_name(t) for t, text in tests.items() if _names_file(text, path.removeprefix("tools/nzfleet/"))}
    return sorted(f"tests.{name}" for name in chosen)


def _importers(module: str, modules: dict) -> set:
    """`module` and every module of the package that imports it, directly or through another."""
    reaching, pending = {module}, [module]
    while pending:
        current = pending.pop()
        for path, text in modules.items():
            if (found := _FLEET_MODULE.match(path)) and found[1] not in reaching and _imports(text, current):
                reaching.add(found[1])
                pending.append(found[1])
    return reaching


def _names_file(text: str, relative: str) -> bool:
    """Whether `text` writes the path below the fleet's directory, or the file's own name when it has an extension."""
    name = relative.rsplit("/", 1)[-1]
    return relative in text or ("." in name and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text) is not None)


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


def console_specs(paths: list, modules: dict, coverage: dict | None = None) -> list:
    """The browser specs reached by a changed source, or every spec when coverage cannot prove the selection."""
    from ..devloop import plan

    changed = {path for path in paths if path}
    specs = sorted(path for path in modules if path.startswith(plan.ADMIN + "e2e/") and path.endswith(".spec.ts")
                   and not path.endswith(("api.spec.ts", "crawl.spec.ts", "long-tasks.spec.ts")))
    if coverage is None or set(coverage) != {spec.removeprefix(plan.ADMIN + "e2e/") for spec in specs
                                                  if spec.removeprefix(plan.ADMIN + "e2e/") not in
                                                  {"api.spec.ts", "crawl.spec.ts", "long-tasks.spec.ts"}}:
        return [spec.removeprefix(plan.ADMIN) for spec in specs]
    if any(path.startswith(plan.ADMIN + "e2e/") and not path.endswith(".ts") for path in changed):
        return [spec.removeprefix(plan.ADMIN) for spec in specs]
    if any(path.startswith(plan.ADMIN) and not path.startswith(plan.ADMIN + "e2e/") and
           (not path.startswith(plan.ADMIN + "src/") or
            path.endswith(".css") or path.startswith(plan.ADMIN + "src/app/") or
            path == plan.ADMIN + "src/components/layout.tsx" or
            not any(path.removeprefix(plan.ADMIN) in sources for sources in coverage.values()))
           for path in changed):
        return [spec.removeprefix(plan.ADMIN) for spec in specs]
    importers = {}
    extensions = source_extensions()
    for importer, text in modules.items():
        if not importer.startswith(plan.ADMIN + "src/"):
            continue
        for named in _CONSOLE_IMPORT.findall(text):
            base = plan.ADMIN + "src/" + named[2:] if named.startswith("@/") else importer.rsplit("/", 1)[0] + "/" + named
            joined = plan.normalise(base)
            for candidate in (joined, *(joined + ext for ext in extensions),
                              *(f"{joined}/index{ext}" for ext in extensions)):
                if candidate in modules:
                    importers.setdefault(candidate, set()).add(importer)
                    break
    reaching = set(changed)
    pending = list(changed)
    while pending:
        for importer in importers.get(pending.pop(), ()):
            if importer not in reaching:
                reaching.add(importer)
                pending.append(importer)
    return [spec.removeprefix(plan.ADMIN) for spec in specs if spec in changed or
            plan.closure(spec, modules) & changed or
            any(plan.ADMIN + source in reaching for source in coverage.get(spec.removeprefix(plan.ADMIN + "e2e/"), []))]


def deploy(paths: list, checks: list) -> list:
    """The titles of the deploy checks that can notice a change.

    Each check is a dict with its `title`, the repository paths it `files` runs and the `prefixes` it reads, the `text` of everything it
    reads, and `always` for a guard that scans the whole tree. A check is related to a path when it runs the
    file, writes its name or path, or names a directory the path is under as a directory (`dir/*`, `dir`).
    """
    chosen = []
    for check in checks:
        if check.get("always") or any(_reads(check, path) for path in paths if path):
            chosen.append(check["title"])
    return chosen


def _reads(check: dict, path: str) -> bool:
    if path in check["files"] or path.startswith(tuple(check["prefixes"])):
        return True
    text = check["text"]
    name = path.rsplit("/", 1)[-1]
    if path in text or ("." in name and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text)):
        return True
    parts = path.split("/")[:-1]
    return any(re.search(rf"(?<![\w.-]){re.escape('/'.join(parts[:n]))}(/\*|/?(?![\w./-]))", text)
               for n in range(2, len(parts) + 1))
