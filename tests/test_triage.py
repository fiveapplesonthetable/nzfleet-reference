import datetime
import os
import tempfile
import unittest
from unittest import mock

from nzfleet import batch, jev, logtail, state, triage, world
from tests import test_batch
from tests.test_batch import FakeChecks
from tests.test_jev import response

NOW = datetime.datetime(2026, 10, 16, 20, 36, 41, tzinfo=datetime.timezone.utc)
RED = {"rc": 1, "log": "test worker::tests::slow ... FAILED\nBlocking waiting for file lock on build directory", "line": "failed: rust",
       "tail": ["test worker::tests::slow ... FAILED"], "lock_waits": ["build directory"], "lock_holders": ["cargo"], "load": 40.0}
TRAIN = {"members": 3, "rerun_allowed": True, "ledger": {"worker::tests::slow": {"count": 1}}}


def probabilities(**given):
    rest = (1 - sum(given.values())) / (6 - len(given))
    return {name: given.get(name, rest) for name in ("defect", "flake", "machine", "stale_branch", "semantic_conflict", "stop")}


def typesafe(*choices, confidence=0.9, failing=None):
    """Jev over a mocked transport that picks the first of `choices` the question's menu offers, else stop."""
    import json
    sent = []

    def transport(url, headers, body):
        sent.append(json.loads(body))
        if failing:
            raise failing
        menu = list(sent[-1]["questions"]["next_action"]["criteria"])
        pick = next((c for c in choices if c in menu), "stop")
        return response(pick, {m: 0.9 if m == pick else 0.1 / (len(menu) - 1) for m in menu}, confidence)
    transport.sent = sent
    return jev.TypeSafe(transport, key=lambda: "k", sleep=lambda _s: None), transport


class Scrubbing(unittest.TestCase):
    def test_paths_hosts_addresses_numbers_and_tokens_do_not_survive(self):
        line = "error at /home/owner/work/nzela/src/main.rs:4 from api.example.com 10.0.0.7:5432 call +243 812 345 678 token=abc123def456"
        out = logtail.scrub(line)
        for gone in ("/home/owner", "api.example.com", "10.0.0.7", "812 345", "abc123def456"):
            self.assertNotIn(gone, out)
        self.assertIn("main.rs:4", out)

    def test_the_tail_is_the_last_sixty_lines_without_blanks(self):
        out = logtail.tail("\n".join(f"line {i}\n" for i in range(100)))
        self.assertEqual((len(out), out[0], out[-1]), (60, "line 40", "line 99"))

    def test_lock_waits_are_named_once(self):
        log = "Blocking waiting for file lock on package cache\nBlocking waiting for file lock on package cache\n"
        self.assertEqual(logtail.lock_waits(log), ["package cache"])

    def test_only_build_tools_holding_a_write_lock_are_named(self):
        locks = "1: FLOCK  ADVISORY  WRITE 11 08:01:5 0 EOF\n2: FLOCK  ADVISORY  READ 12 08:01:6 0 EOF\n3: POSIX  ADVISORY  WRITE 13 08:01:7 0 EOF\n"
        names = {11: "cargo", 12: "rustc", 13: "bash"}
        self.assertEqual(logtail.lock_holders(locks, lambda pid: names[pid]), ["cargo"])


class Outcomes(unittest.TestCase):
    def test_a_flake_is_right_when_the_rerun_is_green(self):
        self.assertEqual(triage.cause_outcome("flake", triage.Facts(rerun_green=True)), "ok")
        self.assertEqual(triage.cause_outcome("flake", triage.Facts(rerun_green=False)), "wrong")

    def test_a_defect_is_wrong_when_the_rerun_is_green(self):
        self.assertEqual(triage.cause_outcome("defect", triage.Facts(rerun_green=True)), "wrong")
        self.assertEqual(triage.cause_outcome("defect", triage.Facts(rerun_green=False)), "ok")

    def test_sending_back_is_right_only_when_the_bisection_names_a_member(self):
        self.assertEqual(triage.action_outcome("send_back", triage.Facts(rerun_green=False, blamed=(4,))), "ok")
        self.assertEqual(triage.action_outcome("send_back", triage.Facts(rerun_green=True)), "wrong")
        self.assertEqual(triage.action_outcome("send_back", triage.Facts(rerun_green=False)), "unknown")

    def test_a_rerun_that_stays_red_was_wasted(self):
        self.assertEqual(triage.action_outcome("rerun_area", triage.Facts(rerun_green=False)), "wrong")

    def test_a_test_that_passes_on_rerun_confirms_flake_and_refutes_defect(self):
        facts = triage.Facts(rerun_green=True, still_failing=frozenset({"other"}))
        self.assertEqual((triage.flake_outcome("flake", "t", facts), triage.flake_outcome("defect", "t", facts)), ("ok", "wrong"))

    def test_load_needs_a_loaded_machine_to_be_confirmed(self):
        self.assertEqual(triage.flake_outcome("load", "t", triage.Facts(rerun_green=True)), "unknown")
        self.assertEqual(triage.flake_outcome("load", "t", triage.Facts(rerun_green=True, load_high=True)), "ok")

    def test_stop_is_never_judged(self):
        self.assertEqual(triage.action_outcome("stop", triage.Facts(rerun_green=True)), "unknown")


class Defaults(unittest.TestCase):
    def test_every_point_acts_and_only_send_back_and_drop_member_are_held_back(self):
        points = [triage.CAUSE, triage.ACTION, triage.FLAKY_TEST]
        self.assertEqual({p.name: p.mode for p in points}, {p.name: jev.Mode.ACT for p in points})
        self.assertEqual(triage.ACTION.irreversible, {"send_back", "drop_member"})
        self.assertEqual(triage.CAUSE.irreversible | triage.FLAKY_TEST.irreversible, frozenset())


class Asking(unittest.TestCase):
    def setUp(self):
        self.log = jev.Log(tempfile.mkdtemp())

    def triage(self, *choices, **kw):
        decider, transport = typesafe(*choices, **kw)
        return triage.Triage(decider, self.log, lambda: NOW), transport

    def test_a_train_failure_is_logged_with_the_rule_beside_jevs_choice(self):
        asked, _ = self.triage("defect")
        asked.red_area(["rust"], RED, TRAIN)
        record = next(r for r in self.log.read() if r["point"] == "batch.red_cause")
        self.assertEqual((record["rule"], record["jev"], record["acted"]), ("flake", "defect", "rule"))

    def test_the_scrubbed_tail_and_lock_holders_are_what_leaves_the_machine(self):
        asked, transport = self.triage("flake")
        asked.red_area(["rust"], RED, TRAIN)
        sent = transport.sent[0]["state"]
        self.assertEqual((sent["tail"], sent["lock_holders"], sent["lock_waits"]), (RED["tail"], ["cargo"], ["build directory"]))
        self.assertNotIn("log", sent)

    def test_the_action_is_asked_several_times_and_each_answer_is_logged(self):
        asked, _ = self.triage("flake")
        asked.red_area(["rust"], RED, TRAIN)
        self.assertEqual(sum(r["point"] == "batch.red_action" for r in self.log.read()), triage.SAMPLES)

    def test_only_confident_rerun_or_bisection_answers_can_be_acted_on(self):
        with mock.patch("nzfleet.config.JEV_ACT", True):
            asked, _ = self.triage("rerun_area")
            result = asked.red_area(["rust"], RED, TRAIN)
            self.assertEqual(result.action, "rerun_area")
            acted = [r for r in self.log.read() if r["point"] == "batch.red_action" and r["acted"] == "jev"]
            self.assertEqual(len(acted), triage.SAMPLES)
            self.assertTrue(all(r["rule"] == "rerun_area" for r in acted))

    def test_a_low_confidence_red_action_stays_with_the_existing_rule(self):
        with mock.patch("nzfleet.config.JEV_ACT", True):
            asked, _ = self.triage("rerun_area", confidence=0.79)
            result = asked.red_area(["rust"], RED, TRAIN)
            self.assertIsNone(result.action)
            self.assertTrue(all(r["acted"] is None and r["escalation"] for r in self.log.read()
                                if r["point"] == "batch.red_action"))

    def test_each_failed_test_is_classified(self):
        asked, _ = self.triage("flake")
        asked.red_area(["rust"], RED, TRAIN)
        self.assertEqual([r["state"]["test"] for r in self.log.read() if r["point"] == "batch.test_flake"], ["worker::tests::slow"])

    def test_one_failed_call_ends_the_asking_for_that_batch(self):
        asked, transport = self.triage(failing=jev.Transient("500"))
        self.assertEqual(len(asked.red_area(["rust", "console"], RED, TRAIN).ids), 1)
        self.assertEqual(len(transport.sent), jev.ATTEMPTS)

    def test_settling_judges_the_rule_and_jev_separately(self):
        asked, _ = self.triage("defect")
        found = asked.red_area(["rust"], RED, {**TRAIN, "ledger": {}})
        asked.settle(found, triage.Facts(rerun_green=True))
        record = next(r for r in self.log.read() if r["point"] == "batch.red_cause")
        self.assertEqual((record["outcome"], record["jev_outcome"]), ("ok", "wrong"))


class InBatches(unittest.TestCase):
    start = test_batch.Runs.start

    def setUp(self):
        test_batch.Runs.setUp(self)
        self.log = jev.Log(tempfile.mkdtemp())
        self.triage = triage.Triage(typesafe("flake", "rerun_area", "flake")[0], self.log, lambda: NOW)

    def run_batch(self, check, members=(4, 5)):
        self.start(members)
        batch.run("b", areas_of=lambda shas: frozenset(), check=check, triage=self.triage)
        return self.log.read()

    def test_a_flake_that_passes_on_rerun_is_judged_right_for_the_rule_and_for_jev(self):
        causes = [r for r in self.run_batch(FakeChecks(flaky=True)) if r["point"] == "batch.red_cause"]
        self.assertEqual([(r["rule"], r["jev"], r["outcome"], r["jev_outcome"]) for r in causes], [("flake", "flake", "ok", "ok")])

    def test_a_broken_member_is_judged_by_the_bisection(self):
        actions = [r for r in self.run_batch(FakeChecks(broken={5})) if r["point"] == "batch.red_action"]
        self.assertEqual({r["rule"] for r in actions}, {"rerun_area"})
        self.assertEqual({r["outcome"] for r in actions}, {"wrong"})

    def test_a_confident_bisect_choice_skips_the_unchanged_rerun(self):
        with mock.patch("nzfleet.config.JEV_ACT", True):
            self.triage = triage.Triage(typesafe("flake", "bisect_now", "flake")[0], self.log, lambda: NOW)
            self.start((4, 5))
            checks = FakeChecks(broken={5})
            batch.run("b", areas_of=lambda shas: frozenset(), check=checks, triage=self.triage)
        self.assertEqual(checks.calls.count([4, 5]), 1)

    def test_a_stopped_red_action_becomes_needs_attention_without_a_rerun(self):
        with mock.patch("nzfleet.config.JEV_ACT", True):
            self.triage = triage.Triage(typesafe("stop")[0], self.log, lambda: NOW)
            self.start((4, 5))
            checks = FakeChecks(broken={5})
            batch.run("b", areas_of=lambda shas: frozenset(), check=checks, triage=self.triage)
        self.assertEqual(len(checks.calls), 1)
        self.assertIn("jev:batch:b", state.read()["needs_person"])

    def test_a_single_member_is_judged_by_its_own_blame(self):
        causes = [r for r in self.run_batch(FakeChecks(broken={4}), members=(4,)) if r["point"] == "batch.red_cause"]
        self.assertEqual([(r["rule"], r["outcome"]) for r in causes], [("defect", "ok")])

    def test_a_batch_without_triage_writes_no_log(self):
        self.start((4, 5))
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(flaky=True))
        self.assertEqual(self.log.read(), [])


class Reporting(unittest.TestCase):
    def test_the_report_lists_disagreements_with_both_outcomes_inside_the_window(self):
        log = jev.Log(tempfile.mkdtemp())
        for stamp, choice in (("2026-10-15 08:00", "defect"), ("2026-10-16 20:00", "defect")):
            asked = triage.Triage(typesafe(choice)[0], log,
                                  lambda s=stamp: datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc))
            asked.settle(asked.red_area(["rust"], RED, TRAIN), triage.Facts(rerun_green=True))
        args = mock.Mock(since="2026-10-16", until=None, point="batch.red_cause", disagreements=5)
        with mock.patch("builtins.print") as shown:
            self.assertEqual(jev.report_command(args, log, NOW), 0)
        text = shown.call_args[0][0]
        self.assertIn("batch.red_cause: 1 calls, agreement 0/1 (0%), mean confidence 0.90", text)
        self.assertIn("rule flake (ok), Jev defect at 0.90 (wrong)", text)
        self.assertNotIn("batch.red_action", text)

    def test_an_unreadable_window_is_refused(self):
        args = mock.Mock(since="soon", until=None, point=None, disagreements=5)
        with mock.patch("builtins.print"):
            self.assertEqual(jev.report_command(args, jev.Log(tempfile.mkdtemp()), NOW), 2)
