#!/usr/bin/env python3
"""Record and consume checked asset observations for local console feedback."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
ADMIN = ROOT / "frontend/admin"
RECEIPT = ADMIN / "affected-receipt.json"
VERSION = 1
LEAF = re.compile(r"frontend/admin/src/screens/[A-Za-z]+\.tsx\Z")
ASSET = re.compile(r"assets/[^/?#]+\Z")
PINNED = (
    "frontend/admin/package-lock.json", "frontend/admin/package.json",
    "frontend/admin/playwright.config.ts", "frontend/admin/playwright.workers.ts",
    "frontend/admin/vite.config.ts", "frontend/admin/tsconfig.app.json",
    "frontend/admin/scripts/screenshots.mjs", "frontend/admin/scripts/shot-run.mjs",
    "frontend/admin/scripts/capture.mjs",
    "frontend/admin/scripts/capture.sh", "frontend/admin/scripts/shots.sh",
    "frontend/admin/scripts/affected-build.ts", "tools/nzfleet/shots/console-receipt.py",
    "scripts/areas/console.sh", "scripts/checks.sh", "tools/nzfleet/nzfleet/selection/changed_tests.py", "tools/nzfleet/shots/visual-plan.py",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def git(*args):
    return subprocess.check_output(("git", *args), cwd=ROOT).decode().strip()


def tracked_inputs():
    names = set(PINNED)
    for prefix in ("frontend/admin/e2e", "frontend/admin/scripts", "scripts", "tools/nzfleet/shots"):
        names.update(git("ls-files", "--", prefix).splitlines())
    return sorted(names)


def fingerprints():
    return {name: digest((ROOT / name).read_bytes()) for name in tracked_inputs()}


def execution_pins():
    lock = json.loads((ADMIN / "package-lock.json").read_text())
    return {"node": subprocess.check_output(("node", "--version"), text=True).strip(),
            "python": sys.version.split()[0],
            "playwright": lock["packages"]["node_modules/playwright"]["version"]}


def canonical_asset_bytes(data, references, keys):
    for name in sorted(references, key=lambda entry: -len(entry)):
        data = data.replace(name.rsplit("/", 1)[-1].encode(), f"@{keys[name]}@".encode())
    return data


def build_assets():
    dist = ADMIN / "dist"
    metadata = json.loads((dist / "affected-build.json").read_text())
    if metadata.get("version") != VERSION or not isinstance(metadata.get("chunks"), dict):
        raise ValueError("missing build module map")
    chunks = metadata["chunks"]
    all_files = sorted(path.relative_to(dist).as_posix() for path in dist.rglob("*") if path.is_file())
    if set(chunks) - set(all_files) or "index.html" not in all_files:
        raise ValueError("incomplete build module map")
    keys = {}
    for name, chunk in chunks.items():
        modules = chunk.get("modules")
        if not isinstance(modules, list) or modules != sorted(set(modules)):
            raise ValueError("invalid chunk module set")
        if not modules and not isinstance(chunk.get("compilerName"), str):
            raise ValueError("unnamed generated chunk")
        key = digest(canonical(modules or ["generated", chunk["compilerName"]]))
        if key in keys.values():
            raise ValueError("ambiguous module set")
        keys[name] = key
    for name, chunk in chunks.items():
        if any(dep not in chunks for dep in chunk.get("imports", [])):
            raise ValueError("unmapped dynamic or static import")
    assets = {}
    for name in all_files:
        if name == "affected-build.json":
            continue
        data = (dist / name).read_bytes()
        if name in chunks:
            data = canonical_asset_bytes(data, chunks[name]["imports"], keys)
        elif name == "index.html":
            data = canonical_asset_bytes(data, keys, keys)
        assets[name] = digest(data)
    return chunks, keys, assets


def trace_assets(path, known_assets):
    found = set()
    uncertain = False
    with zipfile.ZipFile(path) as archive:
        network = [name for name in archive.namelist() if name.endswith(".network")]
        if not network:
            return []
        for name in network:
            for line in archive.read(name).splitlines():
                snapshot = json.loads(line).get("snapshot", {})
                request = snapshot.get("request", {})
                url = urlsplit(request.get("url", ""))
                asset = url.path.lstrip("/")
                if not ASSET.fullmatch(asset):
                    continue
                if asset not in known_assets or snapshot.get("response", {}).get("status") != 200:
                    uncertain = True
                    continue
                found.add(asset)
    return [] if uncertain else sorted(found)


def report_cases(report, assets):
    result = {}
    def visit(suite, titles, depth):
        nested = titles + ([suite["title"]] if depth else [])
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("projectName") != "chromium" or test.get("status") != "expected":
                    raise ValueError("unexpected project or skipped test")
                runs = test.get("results", [])
                if len(runs) != 1 or runs[0].get("status") != "passed" or runs[0].get("retry") != 0:
                    raise ValueError("test did not pass once")
                path = next((item.get("path") for item in runs[0].get("attachments", []) if item.get("name") == "trace"), None)
                if not path:
                    raise ValueError("test has no trace")
                case = f"[chromium] › {suite['file']}:{spec['line']}:{spec['column']} › " + " › ".join(nested + [spec["title"]])
                if case in result:
                    raise ValueError("duplicate case identity")
                result[case] = trace_assets(path, assets)
        for child in suite.get("suites", []):
            visit(child, nested, depth + 1)
    for suite in report.get("suites", []):
        visit(suite, [], 0)
    if report.get("errors") or not result:
        raise ValueError("empty or failed browser report")
    return result


def expected_cases(report):
    result = set()
    def visit(suite, titles, depth):
        nested = titles + ([suite["title"]] if depth else [])
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("projectName") != "chromium":
                    continue
                result.add(f"[chromium] › {suite['file']}:{spec['line']}:{spec['column']} › " + " › ".join(nested + [spec["title"]]))
        for child in suite.get("suites", []):
            visit(child, nested, depth + 1)
    for suite in report.get("suites", []):
        visit(suite, [], 0)
    return result


def record(args):
    chunks, keys, assets = build_assets()
    report = json.loads(Path(args.report).read_text())
    listed = json.loads(Path(args.list_report).read_text())
    cases = report_cases(report, assets)
    if set(cases) != expected_cases(listed):
        raise ValueError("browser run does not cover the discovered catalogue")
    pin = json.loads((Path(args.shot_dir) / "pin.json").read_text())
    lock = json.loads((ADMIN / "package-lock.json").read_text())
    if pin.get("driverVersion") != lock["packages"]["node_modules/playwright"]["version"]:
        raise ValueError("capture did not use the pinned Playwright image")
    shot_files = sorted(file for file in Path(args.shot_dir).glob("*.json") if file.name not in {"pin.json", "complete.json"})
    complete = json.loads((Path(args.shot_dir) / "complete.json").read_text())
    if (complete.get("version") != VERSION or complete.get("files") != [file.name for file in shot_files]
            or complete.get("hashes") != [digest(file.read_bytes()) for file in shot_files]):
        raise ValueError("capture did not finish with these observations")
    shots = {}
    for file in shot_files:
        shot = json.loads(file.read_text())
        stem = shot["name"] + ("-dark" if shot["theme"] == "dark" else "")
        requested = shot["assets"]
        if (stem != file.stem or stem in shots or requested != sorted(set(requested))
                or any(item not in assets for item in requested)
                or (not requested and shot.get("unknown") is not True)
                or (requested and shot.get("unknown", False) is not False)):
            raise ValueError("invalid shot asset observation")
        shots[stem] = requested
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("visual_plan", ROOT / "tools/nzfleet/shots/visual-plan.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    if set(shots) != module.console_catalogue(None):
        raise ValueError("shot run does not cover the registered catalogue")
    names = sorted(assets)
    index = {name: number for number, name in enumerate(names)}
    case_sets = {name: tuple(index[item] for item in values) for name, values in sorted(cases.items())}
    shot_sets = {name: tuple(index[item] for item in values) for name, values in sorted(shots.items())}
    asset_sets = sorted(set(case_sets.values()) | set(shot_sets.values()))
    group = {items: number for number, items in enumerate(asset_sets)}
    receipt = {
        "version": VERSION, "source_commit": git("rev-parse", "HEAD"), "source_tree": git("rev-parse", "HEAD^{tree}"),
        "scope": "chromium built console and pinned light/dark captures", "pins": fingerprints(),
        "execution_pins": execution_pins(),
        "assets": names, "hashes": [assets[name] for name in names],
        "chunks": {keys[name]: {"file": name, "modules": value["modules"], "imports": sorted(keys[dep] for dep in value["imports"])} for name, value in chunks.items()},
        "asset_sets": asset_sets,
        "cases": {name: group[items] for name, items in case_sets.items()},
        "shots": {name: group[items] for name, items in shot_sets.items()},
    }
    RECEIPT.write_bytes(canonical(receipt) + b"\n")
    print(f"recorded {len(cases)} browser cases, {len(shots)} images, {len(names)} assets")


def changed_paths(source_commit):
    fields = subprocess.check_output(("git", "diff", "--name-status", "--no-renames", "-z", source_commit, "--"), cwd=ROOT).split(b"\0")
    changes = [(fields[i].decode(), fields[i + 1].decode()) for i in range(0, len(fields) - 1, 2) if fields[i]]
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    return changes + [("A", name) for name in untracked if not generated_input(name)]


def generated_input(name):
    return name == RECEIPT.relative_to(ROOT).as_posix() or ("/__pycache__/" in name and name.endswith(".pyc"))


def valid_leaf_changes(changes):
    if not changes or any(status != "M" or not LEAF.fullmatch(name) for status, name in changes):
        raise ValueError("input is not a modified direct screen leaf")


def valid_receipt_header(value):
    if value.get("version") != VERSION or value.get("scope") != "chromium built console and pinned light/dark captures":
        raise ValueError("receipt version or scope changed")


def verify_bundle(receipt, changes, chunks, keys, assets):
    recorded_chunks = receipt["chunks"]
    if set(keys) != set(chunks) or set(keys.values()) != set(recorded_chunks):
        raise ValueError("bundle module membership differs")
    by_key = {key: name for name, key in keys.items()}
    for key, old in recorded_chunks.items():
        new_name = by_key[key]
        new = chunks[new_name]
        if old["modules"] != new["modules"] or old["imports"] != sorted(keys[name] for name in new["imports"]):
            raise ValueError("bundle dependency graph differs")
    old_assets = receipt["assets"]
    old_hashes = dict(zip(old_assets, receipt["hashes"], strict=True))
    old_by_key = {key: data["file"] for key, data in recorded_chunks.items()}
    affected = {key for key, data in recorded_chunks.items() if any(name.removeprefix("frontend/admin/") in data["modules"] for _, name in changes)}
    if not affected or len(affected) != len(changes):
        raise ValueError("changed leaf has no unique bundle")
    changed_assets = set()
    for key in affected:
        changed_assets.add(old_by_key[key])
    for name, old_hash in old_hashes.items():
        key = next((key for key, file in old_by_key.items() if file == name), None)
        current_name = by_key[key] if key else name
        if current_name not in assets:
            raise ValueError("built asset is missing")
        if key not in affected and assets[current_name] != old_hash:
            raise ValueError("shared chunk or global asset changed")
    if len(assets) != len(old_assets):
        raise ValueError("built asset set changed")
    renamed = {old_by_key[key]: by_key[key] for key in old_by_key}
    expected_assets = {renamed.get(name, name) for name in old_assets}
    if set(assets) != expected_assets:
        raise ValueError("unexpected built asset")
    if not receipt["cases"] or not receipt["shots"]:
        raise ValueError("empty receipt collection")
    groups = receipt["asset_sets"]
    tuples = [tuple(group) for group in groups]
    if (tuples != sorted(set(tuples))
            or any(list(group) != sorted(set(group))
                   or any(not isinstance(item, int) or item < 0 or item >= len(old_assets) for item in group)
                   for group in groups)):
        raise ValueError("invalid asset set dictionary")
    def impacted(entries):
        if any(not isinstance(group, int) or group < 0 or group >= len(groups) for group in entries.values()):
            raise ValueError("invalid asset set reference")
        return sorted(name for name, group in entries.items()
                      if not groups[group] or any(old_assets[index] in changed_assets for index in groups[group]))
    cases = impacted(receipt["cases"])
    shots = impacted(receipt["shots"])
    if not cases or not shots:
        raise ValueError("no observed consumer; capture cannot prove a green run")
    return {"cases": cases, "shots": sorted({name.removesuffix("-dark") for name in shots}), "images": shots}


def selection():
    receipt = json.loads(RECEIPT.read_text())
    if subprocess.check_output(("git", "show", "HEAD:frontend/admin/affected-receipt.json"), cwd=ROOT) != RECEIPT.read_bytes():
        raise ValueError("receipt differs from the committed baseline")
    valid_receipt_header(receipt)
    if receipt.get("execution_pins") != execution_pins():
        raise ValueError("execution pin differs")
    source_commit = receipt["source_commit"]
    if git("rev-parse", f"{source_commit}^{{tree}}") != receipt["source_tree"]:
        raise ValueError("baseline source tree differs")
    subprocess.check_call(("git", "merge-base", "--is-ancestor", source_commit, "HEAD"), cwd=ROOT)
    if fingerprints() != receipt["pins"]:
        raise ValueError("test, registry, tool, or configuration fingerprint differs")
    changes = [(status, name) for status, name in changed_paths(source_commit)
               if name != RECEIPT.relative_to(ROOT).as_posix()]
    valid_leaf_changes(changes)
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("visual_plan", ROOT / "tools/nzfleet/shots/visual-plan.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for _, name in changes:
        before = subprocess.check_output(("git", "show", f"{source_commit}:{name}"), cwd=ROOT).decode()
        after = (ROOT / name).read_text()
        old_imports, old_uncertain = module.module_specifiers(before)
        new_imports, new_uncertain = module.module_specifiers(after)
        if old_uncertain or new_uncertain or old_imports != new_imports:
            raise ValueError("leaf import syntax or dependencies changed")
    return verify_bundle(receipt, changes, *build_assets())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record")
    record_parser.add_argument("--report", required=True)
    record_parser.add_argument("--list-report", required=True)
    record_parser.add_argument("--shot-dir", required=True)
    select_parser = sub.add_parser("select")
    select_parser.add_argument("--output", required=True)
    verify_parser = sub.add_parser("verify-list")
    verify_parser.add_argument("--report", required=True)
    verify_parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    if args.command == "record":
        record(args)
    elif args.command == "verify-list":
        planned = json.loads(Path(args.plan).read_text())["cases"]
        actual = expected_cases(json.loads(Path(args.report).read_text()))
        if not planned or set(planned) != actual or len(planned) != len(actual):
            raise SystemExit("selected Playwright cases differ from discovery")
        print(f"verified {len(actual)} selected Playwright cases")
    else:
        try:
            result = selection()
        except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            result = {"fallback": str(error)}
        Path(args.output).write_bytes(canonical(result) + b"\n")
        print(json.dumps({"cases": len(result.get("cases", [])), "images": len(result.get("images", [])), "fallback": result.get("fallback")}, sort_keys=True))


if __name__ == "__main__":
    main()
