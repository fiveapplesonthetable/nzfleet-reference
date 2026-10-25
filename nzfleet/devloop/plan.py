"""Maps changed paths to the tests that can notice them.

The rules are the ones the CI selection study used: a crate's tests run for a change in the
crate, a Kotlin test class runs when its names reach a changed file through the import graph,
and anything the graph cannot see (build files, manifests, migrations) escalates instead of
guessing. Pure functions of file contents; quick.py does the reading and the running.
"""

import re
from collections import Counter, defaultdict

ANDROID = "frontend/android/"
ADMIN = "frontend/admin/"
KOTLIN_SOURCE_SETS = ("main", "debug", "release")
KOTLIN_TEST_SETS = ("test", "sharedTest")
KOTLIN_BUILD_INPUT = re.compile(r"(\.gradle|\.kts|\.toml|\.properties|AndroidManifest|/res/|proguard)")
KOTLIN_DECLARATION = re.compile(
    r"\b(class|object|interface|typealias)\s+([A-Za-z_][A-Za-z0-9_]*)|^(?:(?:private|internal|public|inline|suspend)\s+)*(fun|val)\s+(?:<[^>]*>\s*)?(?:[A-Za-z0-9_.]+\.)?([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MIN_TYPE_NAME = 3
MIN_MEMBER_NAME = 5

# Every file that changes what a Rust build or its database means.
RUST_WIDE = re.compile(r"^(Cargo\.(toml|lock)|\.sqlx/|rust-toolchain|crates/[^/]+/(Cargo\.toml|build\.rs|migrations/))")
ROOT_MODULES = {"lib", "main"}
SELECTABLE_FILES = 60
RUST_SOURCE = re.compile(r"^crates/([^/]+)/(src|tests)/(.+)\.rs$")


def rust_plan(changed, packages, sources, hops=1):
    """Which crates, library modules and integration tests to run, and what is left to CI.

    `packages` maps a crate name to the crates that depend on it. `sources` maps every path
    under crates/*/{src,tests} to its text. A crate with SELECTABLE_FILES sources or more is
    selected by module and the others run whole. A module is a first path segment, or two when
    that directory alone holds most of the crate. The modules read one another in a near-cycle,
    so `hops` bounds how far readers of a changed module are followed (None: to a fixed point,
    which is nearly the whole crate).
    """
    changed = [path for path in changed if path.startswith(("crates/", "Cargo.", ".sqlx/", "rust-toolchain"))]
    if not changed:
        return {"mode": "none"}
    if any(RUST_WIDE.match(path) for path in changed):
        return {"mode": "all", "why": "a manifest, lockfile, build script, query cache or migration changed"}
    layout = Layout(sources)
    whole, modules, tests, dependents = set(), set(), set(), set()
    for path in changed:
        match = RUST_SOURCE.match(path)
        if not match:
            return {"mode": "all", "why": f"{path} is not a Rust source file the graph reads"}
        crate, root, rest = match.groups()
        dependents |= set(packages.get(crate, ())) - {crate}
        if root == "tests":
            tests.add((crate, rest.split("/")[0]))
        elif crate not in layout.big or rest in ROOT_MODULES or rest.startswith("bin/"):
            whole.add(crate)
        else:
            modules.add(layout.node(crate, rest))
    modules |= layout.readers(modules, hops)
    tests |= test_binaries_reaching(modules, sources, layout)
    return {
        "mode": "select",
        "whole": sorted(whole),
        "modules": sorted(f"{c}:{'::'.join(m)}" for c, m in modules if m not in layout.deep_roots),
        "tests": sorted(f"{c}:{t}" for c, t in tests),
        "downstream": sorted(dependents - whole),
    }


class Layout:
    """Modules of every crate and which of them read which, taken from the sources alone."""

    def __init__(self, sources):
        files = defaultdict(list)
        for path in sources:
            match = RUST_SOURCE.match(path)
            if match and match.group(2) == "src":
                files[match.group(1)].append(match.group(3))
        self.big = {crate for crate, rest in files.items() if len(rest) >= SELECTABLE_FILES}
        self.deep = {
            (crate, first)
            for crate in self.big
            for first, count in Counter(p.split("/")[0] for p in files[crate] if "/" in p).items()
            if count * 2 > len(files[crate])
        }
        self.deep_roots = {(first,) for _, first in self.deep}
        self.sources = sources
        self.local = {self.node(crate, rest) for crate in self.big for rest in files[crate] if rest not in ROOT_MODULES}

    def node(self, crate, rest):
        """(crate, module path) for a path under src; a sibling `x_tests.rs` belongs to `x`."""
        parts = rest.split("/")
        parts[-1] = parts[-1].removesuffix("_tests") if len(parts) == 1 else parts[-1]
        if len(parts) > 1 and (crate, parts[0]) in self.deep:
            second = parts[1].removesuffix(".rs").removesuffix("_tests")
            return (crate, (parts[0],) if second == "mod" and len(parts) == 2 else (parts[0], second))
        return (crate, (parts[0].removesuffix(".rs"),))

    def uses(self, text, own):
        """Module paths a source names through `crate::` or `<crate>::`, re-exports resolved."""
        found = set()
        for match in re.finditer(r"\b(crate|nzela_[a-z]+|nzela)::", text):
            target = own if match.group(1) == "crate" else match.group(1).replace("_", "-")
            for path in segment_paths(text, match.end(), 2):
                found.add((target, path))
                if not any(n[0] == target and n[1][:1] == path[:1] for n in self.local):
                    found |= {n for n in self.local if n[0] != target and n[1][:1] == path[:1]}
        return found

    def readers(self, modules, hops):
        """Modules, in any crate, that reach a changed module within `hops` steps."""
        reads = defaultdict(set)
        for path, text in self.sources.items():
            match = RUST_SOURCE.match(path)
            if match and match.group(2) == "src" and match.group(1) in self.big and match.group(3) not in ROOT_MODULES:
                reads[self.node(match.group(1), match.group(3))] |= self.uses(text, match.group(1))
        reached = set(modules)
        while hops is None or hops > 0:
            more = {n for n, used in reads.items() if n not in reached and any(overlaps(u, r) for u in used for r in reached)}
            if not more:
                break
            reached |= more
            hops = None if hops is None else hops - 1
        return reached - set(modules)


def overlaps(use, node):
    (crate_a, a), (crate_b, b) = use, node
    return crate_a == crate_b and a[: len(b)] == b[: len(a)]


def segment_paths(text, at, depth):
    """The first `depth` segments of the path at `at`, one tuple per item of a brace group."""
    if text.startswith("{", at):
        found, level, start = set(), 0, at + 1
        for i in range(at, len(text)):
            level += (text[i] == "{") - (text[i] == "}")
            if (text[i] == "," and level == 1) or level == 0:
                found |= segment_paths(text, skip_space(text, start), depth)
                start = i + 1
            if level == 0:
                break
        return found
    path = []
    while len(path) < depth:
        name = IDENTIFIER.match(text, at)
        if not name:
            break
        path.append(name.group(0))
        at = name.end()
        if not text.startswith("::", at):
            break
        at += 2
    return {tuple(path)} if path else set()


def skip_space(text, at):
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def test_binaries_reaching(modules, sources, layout):
    """Integration test binaries whose paths reach a changed module."""
    found = set()
    for path, text in sources.items():
        match = RUST_SOURCE.match(path)
        if match and match.group(2) == "tests" and any(overlaps(u, m) for u in layout.uses(text, match.group(1)) for m in modules):
            found.add((match.group(1), match.group(3).split("/")[0]))
    return found


def kotlin_name_graph(files):
    """Class names declared in main sources, and the identifiers every file mentions."""
    declared = defaultdict(set)
    mentions = {}
    for path, text in files.items():
        mentions[path] = set(IDENTIFIER.findall(text))
        if source_set(path) in KOTLIN_SOURCE_SETS:
            for type_kind, type_name, top_kind, top_name in KOTLIN_DECLARATION.findall(text):
                name = type_name or top_name
                if len(name) >= (MIN_TYPE_NAME if type_name else MIN_MEMBER_NAME):
                    declared[name].add(path)
    return declared, mentions


def source_set(path):
    match = re.match(r"frontend/android/app/src/([^/]+)/", path)
    return match.group(1) if match else None


def android_plan(changed, files, hops=0):
    """Kotlin unit test files whose names reach a changed file, or an escalation.

    `files` maps every Kotlin path under the app's source sets to its text. Names such as
    `Refused` or `terms` are mentioned from everywhere, so `hops` bounds how many times main
    sources that read a changed name are followed (None: to a fixed point, nearly every test).
    """
    changed = [path for path in changed if path.startswith(ANDROID)]
    if not changed:
        return {"mode": "none"}
    for path in changed:
        if not path.endswith(".kt") and KOTLIN_BUILD_INPUT.search(path):
            return {"mode": "all", "why": f"{path} is a build, manifest or resource input"}
    kotlin = [p for p in changed if p.endswith(".kt") and p in files]
    gone = [p for p in changed if p.endswith(".kt") and p not in files and source_set(p)]
    if gone:
        return {"mode": "all", "why": f"{gone[0]} was deleted, so its dependents cannot be read"}
    if not kotlin:
        return {"mode": "none", "note": "no Kotlin unit-test source set file changed"}
    declared, mentions = kotlin_name_graph(files)
    dirty = {p for p in kotlin if source_set(p) in KOTLIN_SOURCE_SETS}
    for _ in range(hops) if hops is not None else iter(int, 1):
        names = {n for n, owners in declared.items() if owners & dirty}
        grown = dirty | {p for p, m in mentions.items() if source_set(p) in KOTLIN_SOURCE_SETS and m & names}
        if grown == dirty:
            break
        dirty = grown
    names = {n for n, owners in declared.items() if owners & dirty}
    chosen = {p for p in kotlin if source_set(p) in KOTLIN_TEST_SETS}
    chosen |= {p for p, m in mentions.items() if source_set(p) in KOTLIN_TEST_SETS and m & names}
    classes = sorted(c for c in (test_class(p, files[p]) for p in chosen) if c)
    return {"mode": "select", "classes": classes}


def test_class(path, text):
    package = re.search(r"^package\s+([\w.]+)", text, re.M)
    if not package or not path.endswith("Test.kt") or "@Test" not in text:
        return None
    return f"{package.group(1)}.{path.rsplit('/', 1)[1].removesuffix('.kt')}"


IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]""")


def console_plan(changed, modules, tests):
    """Console unit tests whose import closure holds a changed module.

    `modules` maps each path under frontend/admin to its text; `tests` lists the unit test paths.
    A test that reads files by name has no import to follow, so it runs for any source change.
    """
    changed = [path for path in changed if path.startswith(ADMIN)]
    if not changed:
        return {"mode": "none"}
    for path in changed:
        if re.search(r"(package(-lock)?\.json|tsconfig[^/]*\.json|vite\.config|\.mjs$)", path) and "/tests/" not in path:
            return {"mode": "all", "why": f"{path} configures the whole console"}
    sources = [p for p in changed if p.startswith(ADMIN + "src/")]
    chosen = {p for p in changed if p in tests}
    for test in tests:
        if sources and (reads_files(modules.get(test, "")) or closure(test, modules) & set(sources)):
            chosen.add(test)
    return {"mode": "select", "tests": sorted(chosen)}


def reads_files(text):
    return "node:fs" in text or "readFileSync" in text


def closure(start, modules):
    seen, pending = set(), [start]
    while pending:
        current = pending.pop()
        for target in imports_of(current, modules):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def imports_of(path, modules):
    base = path.rsplit("/", 1)[0]
    for relative in IMPORT.findall(modules.get(path, "")):
        joined = normalise(f"{base}/{relative}")
        for candidate in (joined, *(joined + ext for ext in (".ts", ".tsx", ".mjs", ".js")), *(f"{joined}/index{ext}" for ext in (".ts", ".tsx"))):
            if candidate in modules:
                yield candidate
                break


def normalise(path):
    out = []
    for part in path.split("/"):
        if part == "..":
            out and out.pop()
        elif part != ".":
            out.append(part)
    return "/".join(out)


def uncovered(changed):
    """Changed paths no quick run reads: they belong to presubmit."""
    own = ("crates/", "Cargo.", ".sqlx/", "rust-toolchain", ANDROID, ADMIN + "src/", ADMIN + "tests/")
    return sorted(p for p in changed if not p.startswith(own))
