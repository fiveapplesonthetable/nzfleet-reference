"""The paths and areas that selection names exist, and a moved file still selects the area of the path it left."""

import os
import re
import subprocess
import tempfile
import unittest

from nzfleet import plan

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SELECTOR = os.path.join(ROOT, "scripts", "changed-areas.sh")


def read(relative: str) -> str:
    with open(os.path.join(ROOT, relative)) as f:
        return f.read()


def tracked() -> set:
    return set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n"))


def literal_paths_of_selector() -> set:
    """Every file a pattern or case arm of changed-areas.sh names outright, with `(a|b)` groups spelled out."""
    script = read("scripts/changed-areas.sh")
    found = set()
    for head, options, ext in re.findall(r"\^((?:[\w-]+/)*)\(([\w|-]+)\)\\\.(\w+)\$", script):
        found.update(f"{head}{option}.{ext}" for option in options.split("|"))
    for path, ext in re.findall(r"\^((?:[\w-]+/)+[\w-]+)\\\.(\w+)\$", script):
        found.add(f"{path}.{ext}")
    for arm in re.findall(r"^\s+((?:[\w./-]+\|)+[\w./-]*)[\\)]", script, re.M):
        found.update(p for p in arm.split("|") if "." in os.path.basename(p) and "*" not in p)
    return found


class SelectionNamesLivePaths(unittest.TestCase):
    def test_every_file_the_selector_names_is_tracked(self):
        named = literal_paths_of_selector()
        self.assertIn("scripts/checks.sh", named)
        self.assertEqual(sorted(p for p in named if p not in tracked()), [])

    def test_every_path_an_allow_list_names_is_tracked(self):
        listed = {line.split("\t")[0] for name in ("no-test-sleeps.allow", "no-android-vague-waits.baseline")
                  for line in read(f"scripts/{name}").splitlines() if line.strip() and not line.startswith("#")}
        self.assertEqual(sorted(p for p in listed if p not in tracked()), [])

    def test_the_planner_reads_the_inputs_the_selector_reads(self):
        script = read("scripts/changed-areas.sh")
        for area, name in (("android", "android_reads"), ("console", "console_reads"), ("console-crawl", "console_reads")):
            declared = re.search(rf"^{name}='([^']*)'", script, re.M).group(1)
            self.assertEqual(plan.TRAIN_INPUTS[area].pattern, declared, area)

    def test_every_area_the_selector_can_select_has_a_gating_output_in_ci(self):
        areas = subprocess.run(["bash", SELECTOR, "all"], capture_output=True, text=True, check=True).stdout.split()
        ci = read(".github/workflows/ci.yaml")
        outputs = set(re.findall(r"^      (\w+): \$\{\{ steps\.areas\.outputs\.\w+ \}\}$", ci, re.M))
        jobs = set(re.findall(r"^  ([\w-]+):$", ci, re.M))
        self.assertEqual(sorted(a for a in areas if a not in outputs | jobs), [])


class AMovedFileSelectsTheAreaItLeft(unittest.TestCase):
    def test_a_rename_out_of_the_console_selects_the_console(self):
        with tempfile.TemporaryDirectory() as repo:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                                      capture_output=True, text=True, check=True).stdout
            git("init", "-q", "-b", "main")
            os.makedirs(os.path.join(repo, "frontend/admin/src"))
            body = "".join(f"line {n}\n" for n in range(40))
            with open(os.path.join(repo, "frontend/admin/src/a.ts"), "w") as f:
                f.write(body)
            git("add", ".")
            git("commit", "-q", "-m", "one")
            os.makedirs(os.path.join(repo, "docs"))
            git("mv", "frontend/admin/src/a.ts", "docs/a.md")
            git("commit", "-q", "-m", "two")
            for script in ("scripts/areas-since.sh",):
                os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
                for name in (script, "scripts/changed-areas.sh"):
                    with open(os.path.join(repo, name), "w") as f:
                        f.write(read(name))
                    os.chmod(os.path.join(repo, name), 0o755)
                git("add", ".")
                git("commit", "-q", "-m", "scripts")
            out = subprocess.run(["bash", "scripts/areas-since.sh", "HEAD~2"], cwd=repo, capture_output=True, text=True)
            self.assertIn("console", out.stdout.split(), out.stderr)

    def test_no_git_diff_that_lists_names_folds_a_rename_into_its_destination(self):
        offenders = []
        for relative in subprocess.run(["git", "grep", "-l", "--", "name-only", "--", "scripts", ".githooks",
                                        "tools/nzfleet/nzfleet", "tools/nzfleet/bin"],
                                       cwd=ROOT, capture_output=True, text=True).stdout.split():
            for number, line in enumerate(read(relative).splitlines(), 1):
                if re.search(r"git.*\bdiff\b.*--name-only|\"diff\",\s*\"--name-only\"", line) and "--no-renames" not in line \
                        and "--diff-filter=U" not in line and not line.lstrip().startswith("#") \
                        and not os.path.basename(relative).startswith("test_") and not relative.endswith("-test.sh"):
                    offenders.append(f"{relative}:{number}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
