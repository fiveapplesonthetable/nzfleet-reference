"""What a train runs: the server's checks always, each client's suite only for what it reads, and never formal."""

import os
import tempfile
import subprocess
import unittest
from unittest import mock

from nzfleet import plan, trainpr, world
from nzfleet.selection import areas as selection

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
APP = "frontend/android/app/src/main/java/app/nzela/ui/amount/AmountContent.kt"
CONSOLE = "frontend/admin/src/screens/Senders.tsx"
ANDROID_SHOT = "frontend/android/docs/screenshots/amount.png"
CONSOLE_SHOT = "frontend/admin/screenshots/02-dashboard.png"
FORMAL = "formal/kani/run.sh"
SERVER = "crates/nzela-providers/src/lib.rs"
APP_ROUTE = "crates/nzela-api/src/quote.rs"
CONSOLE_ROUTE = "crates/nzela-operator-boards/src/reads/home.rs"


def selected(*files, tier="train"):
    return frozenset(selection.select(list(files), tier)[0])


def required(*files):
    return plan.check_areas(selected(*files), list(files), frozenset(), train=True)


class ATrainRunsTheServerAlways(unittest.TestCase):
    def test_a_fleet_change_selects_fleet_without_the_deploy_area(self):
        self.assertEqual(selected("tools/nzfleet/nzfleet/plan.py", tier="change"), {"secrets", "fleet"})

    def test_a_docs_only_train_still_compiles_tests_and_drives_the_machine(self):
        self.assertTrue({"compile", "contract", "rust", "e2e", "secrets"} <= required("docs/api.md"))

    def test_a_server_train_runs_neither_client_suite(self):
        self.assertFalse({"android", "console"} & required(SERVER))


class ATrainRunsAClientOnlyForWhatItReads(unittest.TestCase):
    def test_an_app_change_runs_android_and_not_the_console(self):
        got = required(APP)
        self.assertEqual(("android" in got, "console" in got), (True, False))

    def test_a_console_change_runs_the_console_and_not_android(self):
        got = required(CONSOLE)
        self.assertEqual(("console" in got, "android" in got), (True, False))

    def test_a_route_the_app_reads_runs_android_and_not_the_console(self):
        got = required(APP_ROUTE)
        self.assertEqual(("android" in got, "console" in got), (True, False))

    def test_a_route_the_console_reads_runs_the_console_and_not_android(self):
        got = required(CONSOLE_ROUTE)
        self.assertEqual(("console" in got, "android" in got), (True, False))

    def test_a_screenshot_runs_only_the_frontend_it_belongs_to(self):
        self.assertEqual(required(ANDROID_SHOT) & {"android", "console"}, {"android"})
        self.assertEqual(required(CONSOLE_SHOT) & {"android", "console"}, {"console"})

    def test_a_change_to_the_shared_checks_runs_no_client_suite(self):
        self.assertFalse({"android", "console"} & required("scripts/checks.sh"))

    def test_fuzz_and_crate_changes_select_fuzz_while_crashes_run_nightly(self):
        self.assertIn("fuzz", selected("crates/nzela/src/lib.rs", tier="change"))
        self.assertIn("rust", selected("fuzz/fuzz_targets/api.rs", tier="change"))
        self.assertIn("fuzz", selected("fuzz/fuzz_targets/api.rs", tier="change"))
        self.assertIn("fuzz", selected("docs/api.md", tier="all"))

    def test_a_workflow_that_publishes_shots_leaves_the_sweep_to_main(self):
        self.assertFalse({"android", "console"} & required(".github/workflows/shots.yaml"))

    def test_the_planner_and_the_script_read_the_same_inputs(self):
        for file in (APP, CONSOLE, APP_ROUTE, CONSOLE_ROUTE, SERVER, "scripts/contract.sh", "crates/nzela-api/src/notice.rs"):
            asked = {a for a in ("android", "console") if plan.TRAIN_INPUTS[a].search(file)}
            self.assertEqual(asked, selected(file) & {"android", "console"}, file)

    def test_main_change_detection_includes_both_sides_of_a_rename(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "fixture"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "fixture@example.test"], check=True)
            source = os.path.join(repo, "frontend/admin/src/screen.tsx")
            destination = os.path.join(repo, "frontend/android/app/src/screen.tsx")
            os.makedirs(os.path.dirname(source))
            with open(source, "w") as screen:
                screen.write("export const screen = true\n")
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "base"], check=True)
            base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            os.makedirs(os.path.dirname(destination))
            os.rename(source, destination)
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-qm", "move"], check=True)
            head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            with mock.patch.object(world, "REPO", repo), mock.patch.object(world, "MAIN", base):
                self.assertEqual(world.changed_files(head), [
                    "frontend/admin/src/screen.tsx", "frontend/android/app/src/screen.tsx",
                ])


class ATrainNeverRunsFormal(unittest.TestCase):
    def test_a_proof_change_waits_for_main(self):
        self.assertNotIn("formal", required(FORMAL))

    def test_a_formal_input_runs_after_merge_not_on_a_change(self):
        self.assertNotIn("formal", plan.check_areas(selected(FORMAL, tier="change"), [FORMAL], frozenset()))


class ATrainRequiresWhatItRuns(unittest.TestCase):
    def test_the_areas_a_train_needs_before_its_mail_are_the_ones_it_runs(self):
        files = [CONSOLE, SERVER]
        self.assertEqual(trainpr.required_areas(files, selected(*files), frozenset()),
                         required(*files) | {"console"})



class ATrainDoesNotRunEverythingForOnePathItCannotPlace(unittest.TestCase):
    PATH = "tools/mystery/run.sh"

    def test_a_train_adds_deploy_and_runs_neither_client_suite(self):
        got = frozenset(selection.select([self.PATH], "train")[0])
        self.assertTrue({"deploy", "rust", "e2e"} <= got)
        self.assertFalse({"android", "console", "image"} & got)

    def test_a_train_names_the_path_it_could_not_place(self):
        self.assertEqual(selection.select([self.PATH], "train")[1], (self.PATH,))

    def test_a_single_change_to_an_unplaced_path_runs_deploy_and_not_every_area(self):
        self.assertEqual(frozenset(selection.select([self.PATH], "change")[0]), {"secrets", "deploy"})

    def test_a_single_change_to_an_unplaced_script_runs_every_area(self):
        self.assertIn("android", selection.select(["scripts/nobody-placed-this.sh"], "change")[0])


if __name__ == "__main__":
    unittest.main()
