import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from nzfleet import journeys_plan as plan, journeys_run, state

SETTINGS = plan.Settings("SERIAL-X", "pkg.under.test", "tcp:adb-host:5037", "/keys/jev", "postgres://db/x", 9100,
                         ("app/", "api/"))
OLD, NEW = "a" * 40, "b" * 40
FIXTURE = os.path.join(os.path.dirname(__file__), "testdata", "journeys-run")


def fixture(name):
    with open(os.path.join(FIXTURE, name)) as source:
        return json.load(source)


class WhenARunStarts(unittest.TestCase):
    def decide(self, seen=OLD, head=NEW, changed=("app/x.kt",), running=False, settings=SETTINGS):
        return plan.decide(settings, seen, head, list(changed), running)

    def test_a_change_to_the_app_starts_a_run(self):
        self.assertTrue(self.decide().run)

    def test_a_change_to_the_api_the_app_reads_starts_a_run(self):
        self.assertTrue(self.decide(changed=("api/track.rs",)).run)

    def test_a_change_elsewhere_is_looked_at_and_starts_nothing(self):
        decision = self.decide(changed=("docs/book/x.md",))
        self.assertEqual((decision.run, decision.remember), (False, True))

    def test_a_run_that_is_going_blocks_another_and_forgets_nothing(self):
        decision = self.decide(running=True)
        self.assertEqual((decision.run, decision.remember), (False, False))

    def test_main_standing_still_starts_nothing(self):
        self.assertEqual(self.decide(seen=NEW).run, False)

    def test_the_first_sight_of_main_only_remembers_it(self):
        decision = self.decide(seen="")
        self.assertEqual((decision.run, decision.remember), (False, True))

    def test_no_configured_phone_turns_the_job_off(self):
        off = plan.Settings("", "p", "", "", "", 1, ("app/",))
        self.assertEqual(self.decide(settings=off), plan.Decision(False, False, "no phone is configured"))


class WhatARunDoes(unittest.TestCase):
    STEPS = plan.steps(SETTINGS, "/wt", "/runs", "/lab", "/target")

    def test_the_phone_and_the_app_come_from_the_settings(self):
        for step in self.STEPS:
            env = dict(step.env)
            self.assertEqual(env["DEVICE_LAB_SERIAL"], "SERIAL-X", step.name)
            self.assertEqual(env["ADB_SERVER_SOCKET"], "tcp:adb-host:5037", step.name)
            self.assertEqual(env["NZELA_APP_PACKAGE"], "pkg.under.test", step.name)
            self.assertEqual(env["NZELA_JEV_KEY_FILE"], "/keys/jev", step.name)

    def test_the_drivers_name_the_one_phone(self):
        driving = [s for s in self.STEPS if s.name in ("journeys", "break-it")]
        self.assertEqual([s.argv[-1] for s in driving], ["SERIAL=SERIAL-X"] * 2)

    def test_every_step_says_the_caller_holds_the_phone(self):
        self.assertTrue(all(dict(s.env)["NZELA_DEVICE_LOCK_HELD"] == "1" for s in self.STEPS))

    def test_the_order_is_build_lab_install_journeys_break_it(self):
        self.assertEqual([s.name for s in self.STEPS], ["build", "lab", "install", "journeys", "break-it"])

    def test_the_key_is_named_by_file_and_never_carried_in_a_step(self):
        self.assertNotIn("TYPESAFE_API_KEY", " ".join(k for s in self.STEPS for k, _ in s.env))


class WhatBecomesAnIssue(unittest.TestCase):
    def found(self, name="break-deep_links"):
        return plan.findings_of(name, fixture("summary.json"), fixture("meta.json"), "SERIAL-X")

    def test_a_medium_finding_is_reported_and_a_low_one_is_not(self):
        titles = [f.title for f in self.found()]
        self.assertIn("link another-senders: lands on Home", titles)
        self.assertIn("Back left the screen as it was", titles)
        self.assertNotIn("A setting could not be restored", titles)

    def test_the_issue_carries_the_recording_the_checks_and_the_command_to_repeat_it(self):
        finding = self.found()[0]
        body = plan.issue_body(finding, NEW, "/runs/20261017-break-deep_links", fixture("summary.json")["checks"])
        self.assertIn("/runs/20261017-break-deep_links", body)
        self.assertIn("make break-it SERIAL=SERIAL-X SCENARIOS=deep_links", body)
        self.assertIn("Failed checks:\n- link another-senders: lands on Home", body)

    def test_a_title_an_open_issue_already_has_is_not_filed_again(self):
        titles = {plan.issue_title(f) for f in self.found()}
        self.assertEqual(plan.new_issues(self.found(), lambda t: t in titles), [])

    def test_the_same_finding_twice_in_one_run_is_filed_once(self):
        self.assertEqual(len(plan.new_issues(self.found() * 2, lambda t: False)), len(self.found()))

    def test_a_journey_that_failed_for_want_of_a_key_is_not_the_apps_fault(self):
        summary = {"name": "journey/step_drawers", "status": "failed", "error": "JevError: Jev request failed with HTTP 401",
                   "findings": []}
        self.assertEqual(plan.findings_of("r", summary, {"kind": "journey", "name": "step_drawers"}, "S"), [])

    def test_a_journey_that_stopped_short_is_a_finding_with_its_command(self):
        summary = {"name": "journey/step_drawers", "status": "failed", "error": "JourneyStop: action limit 40 reached",
                   "findings": []}
        (finding,) = plan.findings_of("r", summary, {"kind": "journey", "name": "step_drawers"}, "S")
        self.assertEqual((finding.severity, finding.repro), ("medium", "make journeys SERIAL=S JOURNEY=step_drawers"))

    def test_a_secret_in_a_finding_does_not_reach_the_issue(self):
        finding = plan.Finding("t", "high", "token ghp_" + "a" * 36, "r", "cmd")
        self.assertNotIn("ghp_", plan.issue_body(finding, NEW, "/d", []))


class Machine(unittest.TestCase):
    """The job against a state directory of its own and a host that records what it was asked."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = os.path.realpath(directory.name)
        for patch in (mock.patch.object(state, "HOME", self.home),
                      mock.patch.object(state, "PATH", os.path.join(self.home, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(self.home, "state.lock")),
                      mock.patch.object(journeys_run, "settings", lambda: SETTINGS),
                      mock.patch.object(journeys_run.config, "HOME", self.home),
                      mock.patch.object(journeys_run.config, "WORK", os.path.join(self.home, "work")),
                      mock.patch.object(journeys_run.config, "TARGETS", os.path.join(self.home, "targets"))):
            patch.start()
            self.addCleanup(patch.stop)
        self.head, self.changed, self.live, self.spawned, self.told = NEW, ["app/a.kt"], set(), [], []
        self.host = journeys_run.Host(head=lambda: self.head, changed=lambda old, new: self.changed,
                                      alive=lambda pid: pid in self.live, spawn=self.spawn, now=lambda: 1.0)

    def spawn(self, sha):
        self.spawned.append(sha)
        self.live.add(4242)
        return 4242

    def tick(self):
        journeys_run.tick(self.host, self.told.append)


class WhenTheDaemonTicks(Machine):
    def test_the_first_tick_only_remembers_main(self):
        self.tick()
        self.assertEqual((self.spawned, state.read()["journeys"]["seen"]), ([], NEW))

    def test_main_moving_onto_the_app_starts_one_run_and_says_so(self):
        self.tick()
        self.head = "c" * 40
        self.tick()
        self.assertEqual(self.spawned, ["c" * 40])
        self.assertIn("running them on the phone", self.told[0])

    def test_a_second_move_while_the_run_goes_starts_nothing(self):
        self.tick()
        self.head = "c" * 40
        self.tick()
        self.head = "d" * 40
        self.tick()
        self.assertEqual(len(self.spawned), 1)

    def test_main_moving_onto_something_else_starts_nothing(self):
        self.tick()
        self.head, self.changed = "c" * 40, ["docs/x.md"]
        self.tick()
        self.assertEqual((self.spawned, state.read()["journeys"]["seen"]), ([], "c" * 40))

    def test_a_dry_run_changes_nothing(self):
        journeys_run.tick(self.host, self.told.append, dry_run=True)
        self.assertEqual(state.read()["journeys"], {})


class WhenARunEnds(Machine):
    def setUp(self):
        super().setUp()
        self.steps, self.issues, self.worktrees = [], [], []
        self.host.run_step = self.run_step
        self.held = []
        self.host.hold = self.hold
        self.host.git = lambda *args, **kw: self.worktrees.append(args[:2]) or ""
        self.host.issue_open = lambda title: False
        self.host.create_issue = lambda title, body: self.issues.append((title, body)) or 77

    @contextlib.contextmanager
    def hold(self, path):
        self.held.append(("taken", path, list(self.steps)))
        yield
        self.held.append(("released", path, list(self.steps)))

    def run_step(self, step, log):
        self.steps.append(step.name)
        if step.name == "break-it":
            directory = os.path.join(self.home, "journeys", NEW[:10], "runs", "break-deep_links")
            os.makedirs(directory)
            for name in ("summary.json", "meta.json"):
                with open(os.path.join(directory, name), "w") as out:
                    json.dump(fixture(name), out)
        return 1 if step.name in ("journeys", "break-it") else 0

    def test_findings_become_issues_even_though_the_drivers_exit_non_zero(self):
        journeys_run.run(self.host, self.told.append, NEW)
        self.assertEqual(self.steps, ["build", "lab", "install", "journeys", "break-it", "down"])
        self.assertEqual([t for t, _ in self.issues],
                         ["Phone: link another-senders: lands on Home", "Phone: Back left the screen as it was"])
        self.assertEqual(state.read()["journeys"]["last"]["issues"], [77, 77])

    def test_the_phone_is_held_from_the_lab_to_its_teardown_and_not_while_building(self):
        journeys_run.run(self.host, self.told.append, NEW)
        self.assertEqual([(what, steps[-1] if steps else None) for what, _, steps in self.held],
                         [("taken", "build"), ("released", "down")])

    def test_a_driver_that_died_without_a_run_is_said_so_rather_than_reported_as_clean(self):
        self.host.run_step = lambda step, log: self.steps.append(step.name) or (1 if step.name == "journeys" else 0)
        self.assertEqual(journeys_run.run(self.host, self.told.append, NEW), 1)
        self.assertIn("journeys exited non-zero and left no run", self.told[-1])

    def test_the_lab_is_taken_down_and_the_checkout_removed_when_the_app_did_not_install(self):
        self.host.run_step = lambda step, log: self.steps.append(step.name) or (2 if step.name == "install" else 0)
        self.assertEqual(journeys_run.run(self.host, self.told.append, NEW), 1)
        self.assertEqual(self.steps, ["build", "lab", "install", "down"])
        self.assertIn(("worktree", "remove"), self.worktrees)
        self.assertEqual(self.issues, [])


if __name__ == "__main__":
    unittest.main()
