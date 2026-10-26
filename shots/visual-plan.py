#!/usr/bin/env python3
"""Plan capture work from committed trees or the current worktree."""

import argparse
import json
import re
import subprocess
import hashlib
import posixpath
from pathlib import Path
from pathlib import PurePosixPath

CONSOLE_SOURCE = "frontend/admin/"
CONSOLE_MODULES = "frontend/admin/src/"
ANDROID_SOURCE = "frontend/android/"
ANDROID_SHOTS = "frontend/android/docs/screenshots/"
CONSOLE_CATALOGUE = "frontend/admin/scripts/screenshots.mjs"
ANDROID_CATALOGUE = "frontend/android/docs/screenshots/catalogue.txt"
ANDROID_GOLDEN = "frontend/android/docs/screenshots.golden"
# Screens live in the app and in each Gradle module; a file's place in the UI is the path below this.
ANDROID_UI = "/src/main/java/app/nzela/ui/"
ANDROID_HELP_SCREEN = "frontend/android/feature/settings/src/main/java/app/nzela/ui/help/HelpScreen.kt"
ANDROID_STATES = "frontend/android/app/src/test/java/app/nzela/screenshot/catalogue/"
# Every screen draws these, so a change in one reaches all of them and none is a leaf.
ANDROID_SHARED_UI = ("components", "design", "theme", "nav", "system")


def git(*args, stdin=None):
    return subprocess.check_output(("git", *args), input=stdin)


def resolve(ref):
    return git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def tracked(ref, prefix):
    return {entry.decode() for entry in git("ls-tree", "-r", "--name-only", "-z", ref, "--", prefix).split(b"\0") if entry}


def source_at(ref, path):
    return Path(path).read_text() if ref is None else git("show", f"{ref}:{path}").decode()


def sources_at(ref, paths):
    """Read many files at a ref with one process rather than one `git show` each."""
    paths = sorted(paths)
    stream = git("cat-file", "--batch", stdin="".join(f"{ref}:{path}\n" for path in paths).encode())
    sources, offset = {}, 0
    for path in paths:
        header_end = stream.index(b"\n", offset)
        header = stream[offset:header_end].split()
        offset = header_end + 1
        if header[-1] == b"missing":
            sources[path] = ""
            continue
        size = int(header[2])
        sources[path] = stream[offset:offset + size].decode(errors="replace")
        offset += size + 1
    return sources


def optional_source_at(ref, path):
    try:
        return source_at(ref, path) if ref is None else subprocess.check_output(
            ("git", "show", f"{ref}:{path}"), stderr=subprocess.DEVNULL
        ).decode()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def console_shots(ref):
    source = source_at(ref, CONSOLE_CATALOGUE)
    shots = source.split("const SHOTS = [", 1)[1].split("\n]\n", 1)[0]
    names = re.findall(r"\bname: '([0-9][0-9]-[^']+)'", shots)
    if len(names) != len(set(names)):
        raise ValueError("duplicate console shot registration")
    return names


def console_catalogue(ref):
    names = console_shots(ref)
    stems = {stem for name in names for stem in (name, f"{name}-dark")}
    if len(stems) != 2 * len(names):
        raise ValueError("console shot names collide with dark image stems")
    return stems


def android_catalogue(ref):
    if ref is None:
        names = Path(ANDROID_CATALOGUE).read_text().splitlines()
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("invalid Android shot catalogue")
        return set(names)
    probe = subprocess.run(("git", "cat-file", "-e", f"{ref}:{ANDROID_CATALOGUE}"), stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        return {PurePosixPath(path).stem for path in tracked(ref, ANDROID_SHOTS) if path.endswith(".png")}
    source = git("show", f"{ref}:{ANDROID_CATALOGUE}").decode()
    names = source.splitlines()
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ValueError("invalid Android shot catalogue")
    return set(names)


def tree_sources(ref, prefix):
    """Every file under a prefix, read once so several searches share one pass."""
    if ref is not None:
        return sources_at(ref, tracked(ref, prefix))
    return {path.as_posix(): path.read_text(errors="replace") for path in Path(prefix).rglob("*") if path.is_file()}


def reference_paths(sources, token, regex=False):
    pattern = re.compile(token.replace("[[:space:]]", r"\s") if regex else re.escape(token))
    return {path for path, source in sources.items() if pattern.search(source)}


def console_source_files(ref):
    paths = tracked(ref, CONSOLE_MODULES) if ref is not None else {
        path.as_posix() for path in Path(CONSOLE_MODULES).rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx", ".js", ".jsx"}
    }
    wanted = {path for path in paths if PurePosixPath(path).suffix in {".ts", ".tsx", ".js", ".jsx"}}
    if ref is not None:
        return sources_at(ref, wanted)
    return {path: optional_source_at(ref, path) for path in wanted}


def module_tokens(source):
    tokens = []
    uncertain = False
    index = 0
    while index < len(source):
        start = index
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                return tokens, True
            index = end + 2
            continue
        if char == "/" and tokens and tokens[-1][1] in {"return", "=", "(", ":", ",", "?"}:
            index += 1
            bracket = False
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source[index] == "[":
                    bracket = True
                    index += 1
                elif source[index] == "]":
                    bracket = False
                    index += 1
                elif source[index] == "/" and not bracket:
                    index += 1
                    while index < len(source) and source[index].isalpha():
                        index += 1
                    break
                elif source[index] == "\n":
                    return tokens, True
                else:
                    index += 1
            else:
                return tokens, True
            tokens.append(("regex", "", start, index, False))
            continue
        if char in "'\"`":
            index += 1
            escaped = False
            while index < len(source):
                if source[index] == "\\":
                    escaped = True
                    index += 2
                elif source[index] == char:
                    index += 1
                    break
                else:
                    index += 1
            else:
                return tokens, True
            value = source[start + 1:index - 1]
            if char == "`" and re.search(r"\b(?:import|require)(?![\w-])", value):
                uncertain = True
            tokens.append(("string" if char != "`" else "template", value, start, index, escaped))
            continue
        if char.isalpha() or char in "_$":
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in "_$"):
                index += 1
            tokens.append(("word", source[start:index], start, index, False))
            continue
        tokens.append(("punct", char, start, start + 1, False))
        index += 1
    return tokens, uncertain


def module_specifiers(source):
    """Return literal module paths and whether any module spelling is unresolved."""
    tokens, uncertain = module_tokens(source)
    modules = []
    verification_binding = any(
        [item[1] for item in tokens[index:index + 5]] == ["const", "require", "=", "useRequireVerification", "("]
        for index in range(len(tokens) - 4)
    )
    for position, token in enumerate(tokens):
        kind, value, start, end, _ = token
        if kind != "word" or value not in {"import", "export", "require"}:
            continue
        if position and tokens[position - 1][1] in {".", "?."}:
            continue
        following = tokens[position + 1:]
        if not following:
            uncertain = True
            continue
        next_token = following[0]
        if value == "import" and next_token[1] == ".":
            if ([item[1] for item in following[:5]] != [".", "meta", ".", "env", "."]
                    or len(following) < 6 or following[5][1] not in {"DEV", "VITE_API_BASE_URL"}):
                uncertain = True
            continue
        if next_token[1] == "(":
            argument = following[1] if len(following) > 1 else None
            closing = following[2] if len(following) > 2 else None
            if (source[end:next_token[2]].strip() or argument is None or closing is None
                    or source[next_token[3]:argument[2]].strip()
                    or argument[0] != "string" or argument[4] or closing[1] != ")"):
                uncertain = True
            else:
                modules.append(argument[1])
            continue
        if value == "require":
            declaration = position and tokens[position - 1][1] == "const" and next_token[1] == "="
            verification_property = next_token[1] == "." and len(following) > 1 and following[1][1] in {"isPending", "mutate"}
            if not (verification_binding and (declaration or verification_property)):
                uncertain = True
            continue
        if value == "export":
            offset = 1 if next_token[1] == "type" else 0
            if len(following) <= offset or following[offset][1] not in {"{", "*"}:
                continue
        if next_token[0] == "string" and value == "import":
            if next_token[4]:
                uncertain = True
            else:
                modules.append(next_token[1])
            continue
        if next_token[1] == ":":
            continue
        depth = 0
        found = False
        for candidate in following[:1000]:
            if candidate[1] in {"{", "("}:
                depth += 1
            elif candidate[1] in {"}", ")"}:
                depth -= 1
            if depth == 0 and candidate[1] == "from":
                after = tokens[position + 1 + following.index(candidate) + 1:]
                if after and after[0][0] == "string" and not after[0][4] and not source[candidate[3]:after[0][2]].strip():
                    modules.append(after[0][1])
                else:
                    uncertain = True
                found = True
                break
            if depth < 0 or candidate[1] == ";" or (candidate[1] in {"import", "export"} and candidate[2] > start):
                break
        if not found and value == "import":
            uncertain = True
    return modules, uncertain


def console_external_modules(ref):
    try:
        package = json.loads(optional_source_at(ref, "frontend/admin/package.json"))
    except json.JSONDecodeError:
        return set()
    return set().union(*(package.get(section, {}).keys() for section in ("dependencies", "devDependencies", "peerDependencies")))


def console_aliases_are_known(ref):
    typescript = optional_source_at(ref, "frontend/admin/tsconfig.app.json")
    vite = optional_source_at(ref, "frontend/admin/vite.config.ts")
    paths = re.search(r"""["']?paths["']?\s*:\s*\{([^}]+)\}""", typescript, re.S)
    aliases = re.search(r"\balias\s*:\s*\{([^}]+)\}", vite, re.S)
    path_keys = set(re.findall(r"['\"]([^'\"]+)['\"]\s*:", paths.group(1))) if paths else set()
    vite_keys = set(re.findall(r"['\"]([^'\"]+)['\"]\s*:", aliases.group(1))) if aliases else set()
    return bool(paths and aliases) and path_keys <= {"@/*"} and vite_keys <= {"@"}


def screen_reference_graph(ref):
    """Return screen module consumers and whether any import is unresolved."""
    consumers = {}
    uncertain = not console_aliases_are_known(ref)
    external_modules = console_external_modules(ref)
    for path, source in console_source_files(ref).items():
        modules, unresolved = module_specifiers(source)
        uncertain |= unresolved
        for module in modules:
            normalized = module.replace("\\", "/")
            if normalized.startswith("@/"):
                normalized = "frontend/admin/src/" + normalized[2:]
            elif normalized.startswith("."):
                normalized = str(PurePosixPath(path).parent.joinpath(normalized))
            elif module.startswith("/"):
                uncertain = True
                continue
            else:
                package = "/".join(module.split("/")[:2]) if module.startswith("@") else module.split("/", 1)[0]
                if package not in external_modules:
                    uncertain = True
                continue
            normalized = posixpath.normpath(normalized)
            screen = re.fullmatch(r"frontend/admin/src/screens/([A-Za-z]+)(?:\.tsx)?", normalized)
            if screen:
                consumers.setdefault(screen.group(1), set()).add(path)
            elif normalized in {"frontend/admin/src/screens", "frontend/admin/src/screens/index", "frontend/admin/src/screens/index.ts", "frontend/admin/src/screens/index.tsx"}:
                uncertain = True
    return consumers, uncertain


def screen_references(ref, screen):
    consumers, uncertain = screen_reference_graph(ref)
    return consumers.get(screen, set()), uncertain


def sign_in_references(ref):
    return screen_references(ref, "SignIn")


def kotlin_only(paths):
    """A size baseline or a doc naming a screen does not draw it."""
    return {path for path in paths if path.endswith(".kt")}


def leaf_context(ref):
    android = tree_sources(ref, ANDROID_SOURCE)
    screen_importers, screen_uncertain = screen_reference_graph(ref)
    return {
        "app": optional_source_at(ref, "frontend/admin/src/app/App.tsx"),
        "routes": optional_source_at(ref, "frontend/admin/src/app/routes.ts"),
        "sign_in_importers": screen_importers.get("SignIn", set()),
        "sign_in_uncertain": screen_uncertain,
        "screen_importers": screen_importers,
        "screen_uncertain": screen_uncertain,
        "signed_in": optional_source_at(ref, "frontend/admin/src/screens/lazy.tsx"),
        "sitemap": optional_source_at(ref, "frontend/admin/src/lib/sitemap.ts"),
        "help_states": optional_source_at(ref, "frontend/android/app/src/test/java/app/nzela/screenshot/catalogue/HelpStates.kt"),
        "state_naming": optional_source_at(ref, "frontend/android/app/src/test/java/app/nzela/screenshot/catalogue/Catalogue.kt"),
        "help_screen": optional_source_at(ref, ANDROID_HELP_SCREEN),
        "help_content_users": kotlin_only(reference_paths(android, "HelpContent")),
        "help_screen_users": kotlin_only(reference_paths(android, "HelpScreen")),
        "help_wildcard_import": bool(reference_paths(android, r"import[[:space:]]+app\.nzela\.ui\.help\.\*", regex=True)),
    }


def can_navigate_without_a_verified_route(block):
    return (len(re.findall(r"\broute: '([^']+)'", block)) != 1
            or re.search(r"\b(?:act|wait|setup|after):|=>|\b(?:async|function)\b", block) is not None)


def routed_screen_selection(path, catalogue, context):
    match = re.fullmatch(r"frontend/admin/src/screens/([A-Za-z]+)\.tsx", path)
    if match is None or match.group(1) == "SignIn":
        return set()
    screen = match.group(1)
    signed_in = context.get("signed_in", "")
    imports = re.findall(r"(?:export )?const (\w+) = lazyHeld\(\(\) => import\('@/screens/" + screen +
                         r"'\)\.then\(\(m\) => \(\{ default: m\.\w+ \}\)\)\)", signed_in)
    if len(imports) != 1:
        return set()
    keys = re.findall(r"^\s+(\w+): " + imports[0] + r",$", signed_in, re.MULTILINE)
    if len(keys) != 1:
        return set()
    routes = re.findall(r"\{ path: PATH\.(\w+), screen: '" + keys[0] + r"'", context.get("routes", ""))
    if len(routes) != 1:
        return set()
    literals = re.findall(r"\b" + routes[0] + r": '([^']+)'", context.get("sitemap", ""))
    if len(literals) != 1:
        return set()
    consumers = context.get("screen_importers", {}).get(screen, set())
    uncertain = context.get("screen_uncertain", True)
    if uncertain or consumers != {"frontend/admin/src/screens/lazy.tsx"}:
        return set()
    blocks = re.split(r"(?=\bname: '[0-9][0-9]-)", catalogue.split("const SHOTS = [", 1)[1].split("\n]\n", 1)[0])[1:]
    route = literals[0]
    relevant = [block for block in blocks if re.search(r"\broute: '" + re.escape(route) + r"'", block)]
    other = [block for block in blocks if block not in relevant]
    if not relevant or any(route in block or can_navigate_without_a_verified_route(block) for block in other):
        return set()
    matches = [re.search(r"\bname: '([^']+)'", block) for block in relevant]
    if any(match is None for match in matches):
        return set()
    names = {match.group(1) for match in matches if match is not None}
    if len(names) != len(relevant):
        return set()
    return {stem for name in names for stem in (name, f"{name}-dark")}


def leaf_selection(path, console, android, console_source, context):
    known_stems = {stem for name in console for stem in (name, f"{name}-dark")}
    if console_source and (routed := routed_screen_selection(path, console_source, context)) and routed <= known_stems:
        return "console", routed
    if path == ANDROID_HELP_SCREEN:
        states, source = context.get("help_states", ""), context.get("help_screen", "")
        expected_content = {path, "frontend/android/app/src/test/java/app/nzela/screenshot/catalogue/HelpStates.kt",
                            "frontend/android/app/src/test/java/app/nzela/flow/HelpTransferSummaryTest.kt"}
        expected_screen = {path, "frontend/android/app/src/main/java/app/nzela/ui/nav/NzelaNavHost.kt", "frontend/android/app/src/test/java/app/nzela/flow/HelpFlowTest.kt"}
        if (not context.get("help_wildcard_import", True) and 'screen = "help"' in states and "{ HelpContent(it," in states
                and 'get() = "state-$screen-$case" + (if (locale == Locale.ENGLISH) "-en" else "") + (if (dark) "_NIGHT" else "")' in context.get("state_naming", "")
                and context.get("help_content_users") == expected_content
                and context.get("help_screen_users") == expected_screen):
            previews = set(re.findall(r"internal fun (Help\w+Preview)\(", source))
            stems = {name for name in android if name.startswith("state-help-") or any(name.startswith(f"{preview}.") for preview in previews)}
            if previews and all(any(name.startswith(f"{preview}.") for name in android) for preview in previews) and any(name.startswith("state-help-") for name in stems):
                return "android", stems
    return None


def android_is_dark(stem):
    return stem.endswith("_NIGHT") or re.search(r"-dark(?:-|$)", stem) is not None


def android_golden(ref):
    lines = optional_source_at(ref, ANDROID_GOLDEN).splitlines()
    return {line.removesuffix(".png") for line in lines if line and not line.startswith("#")}


def kotlin_sources(ref, prefix, recursive):
    if ref is not None:
        return sources_at(ref, {path for path in tracked(ref, prefix) if path.endswith(".kt")
                                and (recursive or "/" not in path.removeprefix(prefix))})
    found = Path(prefix).rglob("*.kt") if recursive else Path(prefix).glob("*.kt")
    return {path.as_posix(): path.read_text() for path in found}


def android_leaves(ref, names):
    """Map a screen's own source file to the light shots that show it.

    A file is a leaf when its directory's composables are drawn by a state or a preview and no
    screen in another directory uses one of them. Anything else is left to the fallback.
    """
    sources = {path: text for path, text in kotlin_sources(ref, ANDROID_SOURCE, True).items() if ANDROID_UI in path}
    declared = {path: set(re.findall(r"^\s*(?:internal |public )?fun ([A-Z]\w*)\(", source, re.M)) for path, source in sources.items()}
    drawn = {}
    for source in kotlin_sources(ref, ANDROID_STATES, False).values():
        for block in source.split('screen = "')[1:]:
            drawn.setdefault(block.split('"', 1)[0], set()).update(re.findall(r"\b([A-Z]\w+)\(", block))
    light = {name for name in names if not android_is_dark(name)}
    # A name is shared when it is a whole identifier in some other directory's source; the nav host
    # is how every screen is reached, so it does not count.
    folders = {path: path.rsplit("/", 1)[0] for path in sources}
    folders_using = {}
    for path, text in sources.items():
        if path.split(ANDROID_UI, 1)[1].startswith("nav/"):
            continue
        for identifier in set(re.findall(r"\w+", text)):
            folders_using.setdefault(identifier, set()).add(folders[path])
    leaves = {}
    for path in sources:
        folder = folders[path]
        below = path.split(ANDROID_UI, 1)[1]
        if "/" not in below or below.split("/", 1)[0] in ANDROID_SHARED_UI:
            continue
        beside = [other for other in sources if folders[other] == folder]
        previews = {name for other in beside for name in declared[other] if "Preview" in name}
        own = {name for other in beside for name in declared[other]} - previews
        if any(folders_using.get(name, set()) - {folder} for name in own):
            continue
        screens = {screen for screen, called in drawn.items() if called & own}
        stems = {name for name in light if any(name.startswith(f"state-{screen}-") for screen in screens)
                 or any(name.startswith(f"{preview}.") for preview in previews)}
        if stems:
            leaves[path] = stems
    return leaves


def android_plan_context(ref, names):
    return {"golden": android_golden(ref) & names, "leaves": android_leaves(ref, names),
            "light": {name for name in names if not android_is_dark(name)}}


def plan(base, head, changes, console_before, console_after, android_before, android_after,
         console_source=None, context=None, console_source_before=None, context_before=None,
         android_context=None):
    selected = {"console": set(), "android": set()}
    deleted = {"console": console_before - console_after, "android": android_before - android_after}
    fallback = {"console": False, "android": False}
    reasons = {}
    for status, old, new in changes:
        paths = {old, new} - {None}
        areas = set()
        for path in paths:
            if path.startswith(CONSOLE_SOURCE):
                areas.add("console")
            elif path.startswith(ANDROID_SOURCE):
                areas.add("android")
            elif not path.startswith(("docs/", "deploy/")):
                areas.update(("console", "android"))
        choice = None
        if android_context is not None and status == "M" and old == new and new in android_context["leaves"]:
            choice = ("android", android_context["leaves"][new])
        elif status == "M" and old == new:
            choice = leaf_selection(new, console_names_from_stems(console_after, console_source) if console_source else set(), android_after, console_source, context or {})
            if choice and console_source_before is not None and context_before is not None:
                previous = leaf_selection(old, console_names_from_stems(console_before, console_source_before), android_before,
                                          console_source_before, context_before)
                if previous != choice:
                    choice = None
        if choice and choice[0] in areas:
            selected[choice[0]].update(choice[1])
            reasons[new] = f"{status}: registered leaf screen closure ({len(choice[1])} images)"
        else:
            for area in areas:
                fallback[area] = True
            if areas:
                scope = "light catalogue and golden" if android_context is not None and areas == {"android"} else "full"
                reasons[new or old] = f"{status}: dependency closure may include unregistered, shared, or dynamic inputs; capture {scope} {', '.join(sorted(areas))} catalogue"
    for area, names in (("console", console_after), ("android", android_after)):
        if fallback[area] and (area == "console" or android_context is None):
            selected[area] = names
    if android_context is not None:
        # Dark is drawn for the golden screens only; the nightly run draws the rest.
        selected["android"] |= android_context["golden"]
        if fallback["android"]:
            selected["android"] |= android_context["light"]
        fallback["android"] = False
    return {
        "version": 1,
        "base": base,
        "head": head,
        "selected": {area: sorted(names) for area, names in selected.items()},
        "renderer": {"console": sorted({name for name in console_names_from_stems(selected["console"], console_source)}) if console_source else [], "android": sorted(selected["android"])},
        "deleted": {area: sorted(names) for area, names in deleted.items()},
        "reasons": reasons,
        "full_fallback": fallback,
    }


def console_names_from_stems(stems, source):
    names = set(re.findall(r"\bname: '([0-9][0-9]-[^']+)'", source.split("const SHOTS = [", 1)[1].split("\n]\n", 1)[0]))
    return {name for name in names if name in stems or f"{name}-dark" in stems}


def changes_between(base, head):
    fields = git("diff", "--name-status", "-z", "-M", base, *([] if head is None else [head])).split(b"\0")
    result = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode()
        if status.startswith(("R", "C")):
            result.append((status, fields[index + 1].decode(), fields[index + 2].decode()))
            index += 3
        else:
            old_or_new = fields[index + 1].decode()
            result.append((status, None if status == "A" else old_or_new, None if status == "D" else old_or_new))
            index += 2
    if head is None:
        untracked = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        result += [("A", None, path.decode()) for path in untracked if path and not generated_untracked(path.decode())]
    return result


def generated_untracked(path):
    return ("/__pycache__/" in path or path.endswith(".pyc")
            or (path.startswith("frontend/android/app/") and re.fullmatch(r"hs_err_pid\d+\.log", PurePosixPath(path).name) is not None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--working-tree", action="store_true", help="include modified and untracked files after HEAD")
    args = parser.parse_args()
    base, head = resolve(args.base), resolve(args.head)
    if args.working_tree and head != resolve("HEAD"):
        parser.error("--working-tree requires --head HEAD")
    current = None if args.working_tree else head
    console_before, console_after = console_catalogue(base), console_catalogue(current)
    android_before, android_after = android_catalogue(base), android_catalogue(current)
    changes = changes_between(base, head)
    if args.working_tree:
        changes += changes_between(head, None)
        digest = hashlib.sha256()
        for status, old, new in changes:
            digest.update(f"{status}\0{old}\0{new}\0".encode())
        for path in sorted({p for _, old, new in changes for p in (old, new) if p and Path(p).is_file()}):
            digest.update(path.encode() + b"\0" + hashlib.sha256(Path(path).read_bytes()).digest())
        head = f"WORKTREE:{head}:{digest.hexdigest()}"
    leaf_candidate = any(
        status == "M" and old == new and (
            re.fullmatch(r"frontend/admin/src/screens/[A-Za-z]+\.tsx", new) is not None
            or new == ANDROID_HELP_SCREEN
        ) for status, old, new in changes
    )
    context_after = leaf_context(current) if leaf_candidate else {}
    context_before = leaf_context(base) if leaf_candidate else {}
    result = plan(base, head, changes, console_before, console_after, android_before, android_after,
                  source_at(current, CONSOLE_CATALOGUE), context_after,
                  source_at(base, CONSOLE_CATALOGUE), context_before,
                  android_plan_context(current, android_after))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
