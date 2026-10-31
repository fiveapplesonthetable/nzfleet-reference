import contextlib
import io
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fakeworld import sh
from nzfleet import agents, cli, codehost, plan, selfupdate, setup, state, units, world

REAL_CHECKOUT = codehost.Checkout
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NZFLEET_FILE, OTHER_FILE = "tools/nzfleet/a.py", "crates/nzela/src/a.rs"


class Machine(unittest.TestCase):
    """A repository whose main on the remote moves, and a detached worktree of it that stands for the running code."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.repo, self.code = os.path.join(self.dir, "repo"), os.path.join(self.dir, "code")
        sh(self.dir, "git", "init", "-q", "-b", "main", self.repo)
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            sh(self.repo, "git", "config", key, value)
        self.gain(NZFLEET_FILE)
        sh(self.repo, "git", "worktree", "add", "-q", "--detach", self.code, "main")
        self.said, self.comments, self.restarts, self.tested = [], [], [], []
        self.green_shas, self.tests_pass = set(), True
        self.unit_changes, self.service_restarts, self.unit_syncs = [], [], 0
        for patch in (mock.patch.object(state, "PATH", os.path.join(self.dir, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(self.dir, "state.lock")),
                      mock.patch.object(units, "sync", side_effect=lambda *a, **k: self.unit_synced() or tuple(self.unit_changes)),
                      mock.patch.object(world, "comment", side_effect=lambda n, body: self.comments.append((n, body)))):
            patch.start()
            self.addCleanup(patch.stop)

    def unit_synced(self):
        self.unit_syncs += 1

    def gain(self, path, age=0):
        """A commit on main that changes `path`, `age` seconds old."""
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a") as f:
            f.write("x\n")
        sh(self.repo, "git", "add", "-A")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(time.time()) - age} +0000"}
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=self.repo, env=env, check=True)
        sh(self.repo, "git", "update-ref", f"refs/remotes/{world.MAIN}", "HEAD")
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def checkout(self):
        def test(code):
            self.tested.append(sh(code, "git", "rev-parse", "HEAD"))
            return self.tests_pass, "boom" if not self.tests_pass else ""
        return REAL_CHECKOUT(self.code, green=lambda sha: sha in self.green_shas, test=test, say=self.said.append,
                                 restart=lambda: self.restarts.append(1), service=lambda: self.service_restarts.append(1))

    def head(self):
        return sh(self.code, "git", "rev-parse", "HEAD")


class TheDaemonMovesItsCode(Machine):
    def test_a_checkout_inside_the_tool_directory_sees_and_runs_the_green_update(self):
        self.code = os.path.join(self.code, "tools", "nzfleet")
        new = self.gain(NZFLEET_FILE)
        self.green_shas.add(new)
        selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.tested, self.restarts), (new, [new], [1]))

    def test_main_ci_proves_an_imported_update_without_ci_on_the_member_commit(self):
        old = self.head()
        sh(self.repo, "git", "checkout", "-q", "-b", "update")
        self.gain(NZFLEET_FILE)
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "reset", "-q", "--hard", old)
        sh(self.repo, "git", "merge", "-q", "--no-ff", "update", "-m", "The tool update reaches main")
        merged = sh(self.repo, "git", "rev-parse", "HEAD")
        sh(self.repo, "git", "update-ref", f"refs/remotes/{world.MAIN}", merged)
        self.green_shas.add(merged)
        selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.tested, self.restarts), (merged, [merged], [1]))

    def test_a_green_nzfleet_commit_moves_the_code_tests_it_and_restarts(self):
        new = self.gain(NZFLEET_FILE)
        self.green_shas.add(new)
        selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.tested, self.restarts), (new, [new], [1]))
        self.assertEqual(state.read()["code_moved"]["to"], new)

    def test_a_commit_whose_ci_is_not_green_does_not_move_it(self):
        old = self.head()
        self.gain(NZFLEET_FILE)
        selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.tested, self.restarts), (old, [], []))

    def test_a_green_commit_that_leaves_nzfleet_alone_does_not_move_it(self):
        old = self.head()
        self.green_shas.add(self.gain(OTHER_FILE))
        selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.restarts), (old, []))

    def test_a_failing_self_test_keeps_the_old_code_tells_the_owner_once_and_is_not_retried(self):
        old = self.head()
        self.green_shas.add(self.gain(NZFLEET_FILE))
        self.tests_pass = False
        for _ in range(2):
            selfupdate.advance(self.checkout())
        self.assertEqual((self.head(), self.restarts, len(self.tested)), (old, [], 1))
        self.assertEqual([n for n, _ in self.comments], [695])
        self.assertIn("boom", self.comments[0][1])

    def test_a_checkout_on_a_branch_is_never_moved(self):
        sh(self.code, "git", "checkout", "-q", "-b", "mine")
        self.green_shas.add(self.gain(NZFLEET_FILE))
        selfupdate.advance(self.checkout())
        self.assertEqual((self.restarts, self.tested), ([], []))

    def test_an_old_commit_stays_untested_after_a_newer_one_moved_past_it(self):
        self.gain(NZFLEET_FILE)
        newest = self.gain(NZFLEET_FILE)
        self.green_shas.add(newest)
        selfupdate.advance(self.checkout())
        self.assertEqual(self.tested, [newest])


class TheDaemonReinstallsUnitsThatDrifted(Machine):
    def move_to_green_commit(self):
        self.green_shas.add(self.gain(NZFLEET_FILE))
        selfupdate.advance(self.checkout())

    def test_a_move_reinstalls_the_units_before_the_restart(self):
        self.unit_changes = ["nzfleet-watchdog.timer"]
        self.move_to_green_commit()
        self.assertEqual((self.restarts, self.service_restarts), ([1], []))
        self.assertTrue(any("nzfleet-watchdog.timer" in line for line in self.said))

    def test_a_changed_service_unit_is_applied_by_having_systemd_restart_the_daemon(self):
        self.unit_changes = [units.SERVICE]
        self.move_to_green_commit()
        self.assertEqual((self.restarts, self.service_restarts), ([], [1]))

    def test_a_failed_self_test_leaves_the_installed_units_alone(self):
        self.tests_pass = False
        self.unit_changes = [units.SERVICE]
        self.move_to_green_commit()
        self.assertEqual((self.unit_syncs, self.service_restarts), (0, []))


class TheCodeSaysHowFarBehindItIs(Machine):
    def run_cli(self, *argv):
        out, err = (io.TextIOWrapper(io.BytesIO(), write_through=True) for _ in range(2))
        with mock.patch.object(codehost, "Checkout", side_effect=lambda: self.checkout()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.suppress(SystemExit):
            cli.main(list(argv))
        return out.buffer.getvalue().decode(), err.buffer.getvalue().decode()

    def test_a_command_warns_once_on_stderr_when_behind_main(self):
        self.gain(NZFLEET_FILE)
        _, err = self.run_cli("version")
        self.assertEqual(err.count("behind main by 1 nzfleet commit"), 1)

    def test_version_reinstalls_a_unit_that_differs_and_says_how_to_apply_it(self):
        self.unit_changes = [units.SERVICE]
        out, _ = self.run_cli("version")
        self.assertIn("nzfleet.service differed from bin/ and were reinstalled", out)
        self.assertIn("systemctl --user restart nzfleet.service", out)

    def test_version_says_when_the_units_match(self):
        out, _ = self.run_cli("version")
        self.assertIn("units: match bin/", out)

    def test_a_command_is_quiet_when_up_to_date(self):
        _, err = self.run_cli("version")
        # Garbage from earlier tests can be collected here and warn into the same stderr; only nzfleet's own lines count.
        ours = [line for line in err.splitlines() if "Warning" not in line and not line.startswith((" ", "\t"))]
        self.assertEqual(ours, [])

    def test_version_names_the_commit_main_and_the_distance(self):
        new = self.gain(NZFLEET_FILE)
        out, _ = self.run_cli("version")
        self.assertIn(self.head()[:8], out)
        self.assertIn(f"main is {new[:8]}", out)
        self.assertIn("behind main by 1 nzfleet commit", out)

    def test_behind_for_over_half_an_hour_is_a_needs_attention_line(self):
        self.gain(NZFLEET_FILE, age=40 * 60)
        behind = self.checkout().behind()
        self.assertIn("over 30 min", selfupdate.attention(behind, time.time()))

    def test_behind_for_a_few_minutes_is_not(self):
        self.gain(NZFLEET_FILE, age=5 * 60)
        self.assertEqual(selfupdate.attention(self.checkout().behind(), time.time()), "")

    def test_the_help_names_the_rule_and_nzfleet_version(self):
        out, _ = self.run_cli("--help")
        self.assertIn(selfupdate.RULE, out)
        self.assertIn("nzfleet version", out)


class TheRuleIsWrittenTheSameEverywhere(unittest.TestCase):
    def test_each_place_carries_the_sentence(self):
        for path in ("tools/nzfleet/README.md", "AGENTS.md", "tools/nzfleet/bin/env.template"):
            with open(os.path.join(ROOT, path)) as f:
                self.assertIn(selfupdate.RULE, f.read().replace("\n# ", " "), path)

    def test_a_worker_brief_carries_it(self):
        self.assertIn(selfupdate.RULE, agents.brief("do a thing", "k"))

    def test_setup_says_it(self):
        said = []
        with mock.patch.object(setup, "_read", side_effect=OSError):
            with self.assertRaises(OSError):
                setup.setup(tempfile.mkdtemp(), run=lambda argv, env=None: (0, ""), say=said.append)
        self.assertEqual(said[0], selfupdate.RULE)


class AScreenshotChangeIsNeverReadyWithoutItsChecks(unittest.TestCase):
    SHOTS_PR = [".github/workflows/shots.yaml", "tools/nzfleet/shots/shot-report.sh", "tools/nzfleet/shots/shot-report-test.sh"]

    def test_a_pr_that_changes_the_screenshot_report_requires_both_frontends(self):
        areas = plan.check_areas(frozenset({"deploy", "secrets"}), self.SHOTS_PR, frozenset())
        self.assertGreaterEqual(areas, {"console", "android"})

    def test_a_batch_that_ran_only_deploy_and_secrets_does_not_cover_it(self):
        areas = plan.check_areas(frozenset({"deploy", "secrets"}), self.SHOTS_PR, frozenset())
        self.assertFalse(plan.covered(areas, ["deploy", "secrets"]))

    def test_a_machine_that_skips_an_area_is_not_asked_for_it(self):
        areas = plan.check_areas(frozenset({"deploy"}), self.SHOTS_PR, frozenset({"android"}))
        self.assertNotIn("android", areas)

    def test_an_unrelated_workflow_change_asks_for_no_frontend_checks(self):
        areas = plan.check_areas(frozenset({"deploy"}), [".github/workflows/ci.yaml.bak", "tools/nzfleet/release/release.sh"], frozenset())
        self.assertEqual(areas, {"deploy"})


if __name__ == "__main__":
    unittest.main()


class SetupPointsTheCliAtTheNamedCheckout(unittest.TestCase):
    def test_a_link_to_an_old_checkout_is_repointed(self):
        with tempfile.TemporaryDirectory() as root:
            old, new, bin_dir = (os.path.join(root, n) for n in ("old", "new", "bin"))
            os.makedirs(bin_dir)
            os.symlink(old, os.path.join(bin_dir, "nzfleet"))
            said = setup._link(new, "nzfleet", bin_dir)
            self.assertEqual(os.readlink(os.path.join(bin_dir, "nzfleet")), new)
            self.assertIn("repointed", said)
            self.assertEqual(setup._link(new, "nzfleet", bin_dir), "")
