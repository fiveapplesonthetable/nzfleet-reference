"""`python3 -m nzfleet.selection`: the selection commands ci, the hooks and the batch call.

Standard library only, so ci's select job runs it straight from a checkout.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

from . import areas, changed_tests, files, last_green, nightly, proof, related, since


def _paths() -> list:
    return [line for line in sys.stdin.read().splitlines() if line]


def _areas(args) -> int:
    found, unplaced = areas.select(_paths(), args.tier)
    for path in unplaced:
        print(f"no area is known for {path}", file=sys.stderr)
    print("\n".join(found))
    return 0


def _since(args) -> int:
    print("\n".join(since.since(args.base, args.deployable, args.pull_request, args.train)))
    return 0


def _tests(args) -> int:
    try:
        paths = _paths()
        chosen = (list(changed_tests.MONEY_TESTS) if args.mode == "all" else
                  changed_tests.repeated(args.area, paths) if args.mode == "repeat" else
                  changed_tests.affected(args.area, paths))
        if args.mode == "all" and args.area != "rust":
            raise changed_tests.NoSuchArea(f"no repeated tests for area: {args.area}")
    except changed_tests.NoSuchArea as error:
        print(error, file=sys.stderr)
        return 2
    print("\n".join(chosen))
    return 0


def _read_tree(root: str, suffixes: tuple) -> dict:
    found = {}
    for directory, _, names in os.walk(root):
        if "node_modules" in directory.split(os.sep):
            continue
        for name in names:
            if name.endswith(suffixes):
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    found[path] = handle.read()
    return found


_DEPLOY_CHECK = re.compile(r'^\s*"(?P<title>[^"|]+)\|(?P<command>[^"|]+)(?:\|(?P<reads>[^"]*))?"\s*$')
_PATHLIKE = re.compile(r"[\w./-]+")


def _deploy_checks(script: str) -> list:
    """The checks listed in scripts/areas/deploy.sh as `"title|command|reads"`, with what each reads."""
    checks = []
    for line in _read(script).splitlines():
        if not (found := _DEPLOY_CHECK.match(line)):
            continue
        words = [*found["command"].split(), *(found["reads"] or "").split()]
        files = {word for word in words if os.path.isfile(word)}
        prefixes = {word.rstrip("/") + "/" for word in words if os.path.isdir(word)}
        text = found["command"]
        for path in sorted(files):
            body = _read(path)
            text += "\n" + body
            files |= {w for w in _PATHLIKE.findall(body) if "/" in w and not w.startswith((".", "/")) and os.path.isfile(w)}
        checks.append({"title": found["title"], "files": files, "prefixes": prefixes, "text": text,
                       "always": "*" in (found["reads"] or "").split()})
    return checks


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _related(args) -> int:
    paths = _paths()
    if args.area == "fleet":
        chosen = related.fleet(paths, _read_tree("tools/nzfleet/tests", (".py",)), _read_tree("tools/nzfleet/nzfleet", (".py",)))
    elif args.area == "deploy":
        chosen = related.deploy(paths, _deploy_checks("scripts/areas/deploy.sh"))
    elif args.area == "console-specs":
        extensions = related.source_extensions()
        modules = {**_read_tree("frontend/admin/e2e", (".ts", ".tsx")), **_read_tree("frontend/admin/fixtures", extensions),
                   **_read_tree("frontend/admin/src", extensions)}
        try:
            saved = json.loads(_read("frontend/admin/e2e/source-coverage.json"))
            names = sorted(path.removeprefix("frontend/admin/e2e/") for path in modules
                           if path.endswith(".spec.ts") and not path.endswith(("api.spec.ts", "crawl.spec.ts", "long-tasks.spec.ts")))
            hashes = {name: hashlib.sha256(_read("frontend/admin/e2e/" + name).encode()).hexdigest()
                      for name in names}
            dynamic = {path.removeprefix("frontend/admin/"):
                       hashlib.sha256(text.encode()).hexdigest()
                       for path, text in modules.items() if path.startswith("frontend/admin/src/")
                       and re.search(r"\bimport\s*\(", text)}
            coverage = saved["sources"] if saved["specs"] == hashes and saved["dynamicFiles"] == dynamic else None
        except (OSError, ValueError, KeyError, TypeError):
            coverage = None
        chosen = related.console_specs(paths, modules, coverage)
    else:
        modules = _read_tree("frontend/admin/src", (".ts", ".tsx", ".mjs"))
        modules.update(_read_tree("frontend/admin/tests", (".ts", ".tsx", ".mjs")))
        tests = {p for p in modules if p.startswith("frontend/admin/tests/") and p.endswith(".test.mjs")}
        chosen = related.console(paths, modules, tests)
    print("\n".join(chosen))
    return 0


def _last_green(args) -> int:
    head = last_green.resolve(args.head)
    if head:
        found = last_green.newest(sys.stdin.read(), head)
        if found:
            print(found)
    return 0


def _nightly(args) -> int:
    verdict = "true" if nightly.keeps(os.environ.get("EVENT_NAME", ""), os.environ.get("SCHEDULE", ""),
                                      int(args.london_hour), datetime.datetime.now(datetime.timezone.utc)) else "false"
    print(f"run={verdict}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as out:
        out.write(f"run={verdict}\n")
    return 0


def _tracked(args) -> int:
    try:
        print("\n".join(files.tracked(args.label, args.specs)))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _require(args) -> int:
    try:
        files.require(args.label, args.path)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _record(args) -> int:
    print(proof.record(args.tree, args.base, args.areas.split(), args.image))
    return 0


def _proof(args) -> int:
    return proof.select(args.api, args.tree, args.labels, args.heads)


def _proved(args) -> int:
    print("\n".join(proof.proved(args.claimed.split(), args.moved.split(), args.image)))
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="nzfleet-select")
    sub = top.add_subparsers(dest="command", required=True)
    a = sub.add_parser("areas", help="areas for the paths on stdin")
    a.add_argument("tier", nargs="?", default="change", choices=(*areas.TIERS, "all", "full"))
    a.set_defaults(run=_areas)
    s = sub.add_parser("since", help="areas for the changes since BASE")
    s.add_argument("--deployable", action="store_true")
    s.add_argument("--pull-request", action="store_true")
    s.add_argument("--train", action="store_true")
    s.add_argument("base", nargs="?", default="")
    s.set_defaults(run=_since)
    t = sub.add_parser("tests", help="tests for the paths on stdin")
    t.add_argument("--mode", choices=("repeat", "all", "affected"), default="repeat")
    t.add_argument("area")
    t.set_defaults(run=_tests)
    x = sub.add_parser("related", help="unit tests related to the paths on stdin, run from the repository root")
    x.add_argument("area", choices=("fleet", "console", "console-specs", "deploy"))
    x.set_defaults(run=_related)
    g = sub.add_parser("last-green", help="newest green ancestor from the runs API response on stdin")
    g.add_argument("head", nargs="?", default="HEAD")
    g.set_defaults(run=_last_green)
    n = sub.add_parser("nightly", help="whether this scheduled run is the wanted London hour (EVENT_NAME, SCHEDULE)")
    n.add_argument("london_hour")
    n.set_defaults(run=_nightly)
    f = sub.add_parser("tracked", help="tracked files matching the pathspecs; fails on none")
    f.add_argument("label"), f.add_argument("specs", nargs="+")
    f.set_defaults(run=_tracked)
    q = sub.add_parser("require", help="fails unless PATH is tracked")
    q.add_argument("label"), q.add_argument("path")
    q.set_defaults(run=_require)
    r = sub.add_parser("proof-record")
    r.add_argument("tree"), r.add_argument("base"), r.add_argument("areas"), r.add_argument("image", nargs="?", default="")
    r.set_defaults(run=_record)
    p = sub.add_parser("proof-select")
    p.add_argument("api"), p.add_argument("tree"), p.add_argument("labels"), p.add_argument("heads", nargs="+")
    p.set_defaults(run=_proof)
    d = sub.add_parser("proved", help="the areas main skips, from the run's claimed areas and those moved since")
    d.add_argument("claimed"), d.add_argument("moved"), d.add_argument("image")
    d.set_defaults(run=_proved)
    return top


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
