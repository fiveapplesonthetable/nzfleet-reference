"""Area selection, the tests a change names, the last green ancestor and the nightly hour, each as plain data."""

import datetime
import json
import os
import subprocess
import tempfile
import unittest

from nzfleet.selection import areas, changed_tests, files, last_green, nightly, since

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SELECT = os.path.join(ROOT, "tools", "nzfleet", "bin", "nzfleet-select")


def chosen(*paths, tier="change"):
    return set(areas.select(list(paths), tier)[0])


class AChangeSelectsOnlyTheAreasItTouches(unittest.TestCase):
    def test_a_book_page_runs_only_secrets(self):
        self.assertEqual(chosen("docs/x.md"), {"secrets"})

    def test_a_console_source_runs_the_console_and_not_android(self):
        got = chosen("frontend/admin/src/a.ts")
        self.assertIn("console", got)
        self.assertNotIn("android", got)

    def test_the_selector_itself_runs_every_area(self):
        self.assertEqual(chosen("tools/nzfleet/nzfleet/selection/areas.py"), set(areas.ALL))

    def test_a_runner_script_runs_deploy_and_a_cache_installer_also_runs_rust(self):
        self.assertEqual(chosen("tools/nzfleet/runner/runner-prune.sh"), {"secrets", "fleet", "deploy"})
        self.assertIn("rust", chosen("tools/nzfleet/runner/install-sccache.sh"))

    def test_a_file_no_rule_places_runs_deploy_and_is_named(self):
        found, unplaced = areas.select(["tools/mystery/run.sh"], "change")
        self.assertEqual((set(found), unplaced), ({"secrets", "deploy"}, ("tools/mystery/run.sh",)))

    def test_an_unplaced_script_runs_every_area_until_it_moves(self):
        self.assertEqual(chosen("scripts/nobody-placed-this.sh"), set(areas.ALL))

    def test_every_tracked_file_is_placed_or_a_script_awaiting_its_move(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual([p for p in tracked if p and not p.startswith("scripts/") and not areas._placed(p)], [])

    def test_an_unknown_tier_is_refused(self):
        with self.assertRaises(ValueError):
            areas.select(["a"], "sometimes")


class ASinceSelectionFallsBackToEverything(unittest.TestCase):
    def test_an_empty_base_selects_every_area(self):
        self.assertEqual(since.since(""), areas.ALL)

    def test_an_unknown_base_selects_every_area(self):
        self.assertEqual(since.since("0" * 40), areas.ALL)

    def test_the_deployable_flag_adds_what_deploy_takes(self):
        with tempfile.TemporaryDirectory() as repo:
            def git(*args):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            git("commit", "-q", "--allow-empty", "-m", "one")
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
            os.makedirs(os.path.join(repo, "docs"))
            open(os.path.join(repo, "docs", "a.md"), "w").close()
            git("add", ".")
            git("commit", "-q", "-m", "two")
            old = os.getcwd()
            os.chdir(repo)
            try:
                self.assertEqual(since.since(base), ("secrets",))
                self.assertEqual(since.since(base, deployable=True), ("secrets", "android", "image", "deploy"))
            finally:
                os.chdir(old)


class TheTestsAChangeNamesAreTheOnesItAffects(unittest.TestCase):
    def test_a_crate_test_file_names_its_test_target(self):
        self.assertEqual(changed_tests.affected("rust", ["crates/nzela-core/tests/money.rs"]),
                         ["*"] if not os.path.isfile(os.path.join(ROOT, "crates/nzela-core/tests/money.rs")) else
                         ["nzela-core --test money"])

    def test_a_deleted_crate_file_repeats_every_money_test(self):
        self.assertEqual(changed_tests.repeated("rust", ["crates/nzela-core/src/gone.rs"]), list(changed_tests.MONEY_TESTS))

    def test_a_source_file_names_its_module_path(self):
        with tempfile.TemporaryDirectory() as repo:
            old = os.getcwd()
            os.chdir(repo)
            try:
                os.makedirs("crates/nzela-core/src/a")
                open("crates/nzela-core/src/a/b.rs", "w").close()
                open("crates/nzela-core/src/lib.rs", "w").close()
                self.assertEqual(changed_tests.repeated("rust", ["crates/nzela-core/src/a/b.rs", "crates/nzela-core/src/lib.rs"]),
                                 ["nzela-core a::b"])
            finally:
                os.chdir(old)

    def test_a_source_file_outside_a_test_makes_the_area_run_whole(self):
        with tempfile.TemporaryDirectory() as repo:
            old = os.getcwd()
            os.chdir(repo)
            try:
                os.makedirs("crates/x/src")
                open("crates/x/src/a.rs", "w").close()
                self.assertEqual(changed_tests.affected("rust", ["crates/x/src/a.rs"]), ["*"])
            finally:
                os.chdir(old)

    def test_an_area_with_nothing_to_repeat_is_refused(self):
        with self.assertRaises(changed_tests.NoSuchArea):
            changed_tests.repeated("console", [])


class TrackedFilesFailClosed(unittest.TestCase):
    def test_an_empty_match_is_a_failure(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            old = os.getcwd()
            os.chdir(repo)
            try:
                with self.assertRaises(ValueError):
                    files.tracked("fixture", ["missing/*.sh"])
                with self.assertRaises(ValueError):
                    files.require("fixture", "missing.sh")
            finally:
                os.chdir(old)

    def test_a_git_error_is_a_failure(self):
        with tempfile.TemporaryDirectory() as nowhere:
            old = os.getcwd()
            os.chdir(nowhere)
            try:
                with self.assertRaises(ValueError):
                    files.tracked("fixture", ["*.sh"])
            finally:
                os.chdir(old)

    def test_a_tracked_match_is_returned(self):
        self.assertIn("scripts/checks.sh", files.tracked("fixture", ["scripts/*.sh"]) if os.getcwd() == ROOT else
                      subprocess.run(["git", "ls-files", "scripts/checks.sh"], cwd=ROOT, capture_output=True, text=True).stdout.split())


class TheLastGreenAncestorSkipsTheHeadItself(unittest.TestCase):
    def test_the_newest_green_ancestor_is_chosen_and_the_head_is_skipped(self):
        with tempfile.TemporaryDirectory() as repo:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            git("commit", "-q", "--allow-empty", "-m", "one")
            one = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "two")
            two = git("rev-parse", "HEAD")
            runs = json.dumps({"workflow_runs": [
                {"head_sha": two, "conclusion": "success"},
                {"head_sha": "f" * 40, "conclusion": "success"},
                {"head_sha": one, "conclusion": "success"}]})
            old = os.getcwd()
            os.chdir(repo)
            try:
                self.assertEqual(last_green.newest(runs, two), one)
                self.assertEqual(last_green.newest("not json", two), "")
            finally:
                os.chdir(old)


class ANightlyRunIsKeptAtTheLondonHour(unittest.TestCase):
    SUMMER = datetime.datetime(2026, 7, 1, 12, tzinfo=datetime.timezone.utc)
    WINTER = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)

    def test_in_summer_the_cron_an_hour_earlier_in_utc_is_kept(self):
        self.assertTrue(nightly.keeps("schedule", "7 2 * * *", 3, self.SUMMER))
        self.assertFalse(nightly.keeps("schedule", "7 3 * * *", 3, self.SUMMER))

    def test_in_winter_the_cron_at_the_london_hour_is_kept(self):
        self.assertTrue(nightly.keeps("schedule", "7 3 * * *", 3, self.WINTER))
        self.assertFalse(nightly.keeps("schedule", "7 2 * * *", 3, self.WINTER))

    def test_a_dispatch_is_never_kept(self):
        self.assertFalse(nightly.keeps("workflow_dispatch", "7 3 * * *", 3, self.WINTER))


class TheCommandLineSpeaksForTheModules(unittest.TestCase):
    def test_areas_reads_paths_on_standard_input_and_prints_one_per_line(self):
        out = subprocess.run([SELECT, "areas"], input="docs/x.md\n", capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["secrets"])

    def test_tracked_fails_on_an_empty_match(self):
        done = subprocess.run([SELECT, "tracked", "fixture", "no/such/*.zzz"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)


if __name__ == "__main__":
    unittest.main()
