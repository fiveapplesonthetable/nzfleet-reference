import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from nzfleet import config, jev, jev_points, observe, progress, state, supervise, fixes
from nzfleet.progress import Activity
from nzfleet.supervise import Proc

import test_fix_agent
from test_supervise_tick import MIN, Supervised
from fleet import NOW

SUPERVISE_IDS = ("wait", "nudge", "kill_and_requeue", "kill_and_hold", "stop")


def answer(choice, ids=SUPERVISE_IDS, confidence=0.9):
    odds = {i: (0.9 if i == choice else 0.1 / (len(ids) - 1)) for i in ids}
    return json.dumps({"model": "jev-latest", "answers": {"next_action": {
        "type": "choice", "choice": choice, "confidence": confidence, "probabilities": odds}}}).encode()


def chooser(response):
    sent = []

    def transport(url, headers, body):
        sent.append(json.loads(body))
        if isinstance(response, Exception):
            raise response
        return response
    transport.sent = sent
    decider = jev.TypeSafe(transport, key=lambda: "secret-key-value", sleep=lambda _s: None)
    decider.sent = sent
    return decider


class FixedAnswer(jev.Decider):
    def __init__(self, choice, confidence=0.9):
        self.choice, self.confidence = choice, confidence

    def choose(self, _state, _menu, _instructions):
        return jev.Verdict(self.choice, "jev", self.confidence)


class TheRulesInTheMenusWords(unittest.TestCase):
    def test_supervise_rule_agrees_with_supervise_step_for_every_case(self):
        for verdict in progress.Verdict:
            for nudged in (False, True):
                state_of = {"verdict": verdict.value, "nudged_before": nudged}
                step = supervise.step(verdict, nudged)
                expected = {"none": "wait", "nudge": "nudge", "resume": "kill_and_requeue", "email": "kill_and_hold"}[step.value]
                self.assertEqual(jev_points.supervise_rule(state_of, ()), expected)

    def test_fix_rule_agrees_with_fixes_decide_for_every_case(self):
        for record in (None, {"sha": "head", "told": False}, {"sha": "head", "told": True}):
            for other_head in (False, True):
                for run in ("queued", "running", "done"):
                    failure = {"sha": "other" if other_head else "head", "tests": ["a"], "areas": ["rust"]}
                    seen = jev_points.fix_state(failure, record, run)
                    expected = {"start": "start_fix", "wait": "wait", "tell": "tell_owner", "done": "done"}[fixes.decide(failure, record, run)]
                    if record is None and other_head:
                        continue  # a head with no attempt has no other head to differ from
                    self.assertEqual(jev_points.fix_rule(seen, ()), expected)

    def test_every_field_the_state_carries_is_one_the_point_allows(self):
        act = Activity(1, 2, NOW - 600, NOW - 300, None)
        seen = jev_points.supervise_state(act, progress.Sample(5.0, NOW, NOW - 120, NOW - 900), NOW - 7200, NOW, False, 1,
                                          progress.Current("test", NOW - 1800, "x"), NOW - 60, {"waiting_on": ["futex_wait_queue"], "lock_holder": "cargo"}, True)
        self.assertEqual(set(seen), set(jev_points.SUPERVISE_STATE))
        fixed = jev_points.fix_state({"sha": "s", "tests": ["a", "b"], "areas": ["rust"]}, None, "queued")
        self.assertEqual(set(fixed), set(jev_points.FIX_STATE))

    def test_supervision_act_requires_twenty_choices_with_ninety_five_percent_agreement_and_no_wrong_outcome(self):
        eligible = [{"point": "supervise.step", "rule": "wait", "jev": "wait", "jev_outcome": "ok"} for _ in range(20)]
        self.assertTrue(jev.agreement_allows(eligible, "supervise.step", frozenset({"wait", "nudge"})))
        self.assertFalse(jev.agreement_allows(eligible[:19], "supervise.step", frozenset({"wait", "nudge"})))
        eligible[-1] = {**eligible[-1], "jev": "nudge"}
        self.assertFalse(jev.agreement_allows(eligible, "supervise.step", frozenset({"wait", "nudge"})))
        eligible[-1] = {**eligible[-1], "jev": "wait", "jev_outcome": "wrong"}
        self.assertFalse(jev.agreement_allows(eligible, "supervise.step", frozenset({"wait", "nudge"})))

    def test_selective_act_blocks_choices_outside_its_allow_list(self):
        menu = jev.Menu((("wait", "Wait"), ("nudge", "Nudge"), ("stop", "Stop")))
        rule = jev.Rule(lambda _state, _values: "nudge")
        allowed = jev.Selective(rule, FixedAnswer("wait"), frozenset({"wait", "nudge"}))
        refused = jev.Selective(rule, FixedAnswer("stop"), frozenset({"wait", "nudge"}))
        self.assertEqual(allowed.choose({}, menu, "").choice, "wait")
        self.assertEqual((refused.choose({}, menu, "").choice, refused.choose({}, menu, "").blocked),
                         ("stop", "choice is outside this rollout's allow-list"))

    def test_selective_act_escalates_when_a_choice_misses_the_point_floor(self):
        answer = jev.Selective(jev.Rule(lambda _state, _values: "wait"), FixedAnswer("wait", 0.59),
                               frozenset({"wait", "nudge"}))
        decision = jev.decide(jev_points.SUPERVISE, answer, {"verdict": "MOVING", "nudged_before": False},
                              jev_points.SUPERVISE_OPTIONS, "", jev.Log(),
                              datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc))
        self.assertIsNone(decision.action)
        self.assertIn("below the floor", decision.escalation)

    def test_the_state_says_how_long_the_cpu_has_been_quiet_and_what_it_waits_on(self):
        seen = jev_points.supervise_state(Activity(0, 0, None, None, None), progress.Sample(5.0, NOW, NOW - 3600, NOW - 7200), NOW - 9000, NOW, False, 2,
                                          None, None, {"waiting_on": ["futex_wait_queue"], "lock_holder": "cargo"}, False)
        self.assertEqual((seen["cpu_moved_minutes"], seen["cpu_busy_minutes"], seen["waiting_on"], seen["lock_holder"], seen["attempt"]),
                         (60, 120, ["futex_wait_queue"], "cargo", 2))


class HowAChoiceTurnsOut(unittest.TestCase):
    def test_a_nudge_that_produces_nothing_in_twenty_minutes_is_wrong(self):
        self.assertIsNone(jev_points.outcome("nudge", False, 19 * MIN))
        self.assertEqual(jev_points.outcome("nudge", False, 20 * MIN), "wrong")

    def test_a_nudge_followed_by_work_is_ok(self):
        self.assertEqual(jev_points.outcome("nudge", True, 3 * MIN), "ok")

    def test_waiting_is_wrong_only_when_the_stall_window_passes_with_nothing_written(self):
        self.assertIsNone(jev_points.outcome("wait", False, 44 * MIN))
        self.assertEqual(jev_points.outcome("wait", False, 45 * MIN), "wrong")

    def test_asking_the_owner_has_no_outcome_to_judge(self):
        self.assertEqual(jev_points.outcome("kill_and_hold", False, 0), "unknown")

    def test_a_fix_is_ok_when_the_head_stops_being_red_and_wrong_when_the_owner_had_to_be_told(self):
        self.assertEqual(jev_points.fix_outcome("start_fix", False, False), "ok")
        self.assertIsNone(jev_points.fix_outcome("start_fix", True, False))
        self.assertEqual(jev_points.fix_outcome("start_fix", True, True), "wrong")

    def test_work_since_the_decision_is_a_new_commit_or_a_newer_edit(self):
        self.assertTrue(jev_points.progressed(Activity(2, 1, 5.0, 5.0, None), 10.0, 1))
        self.assertTrue(jev_points.progressed(Activity(1, 1, 5.0, 11.0, None), 10.0, 1))
        self.assertFalse(jev_points.progressed(Activity(1, 1, 5.0, 5.0, None), 10.0, 1))


class WhatAnAgentWaitsOn(unittest.TestCase):
    LOCKS = ("1: POSIX  ADVISORY  WRITE 200 08:01:777 0 EOF\n"
             "2: FLOCK  ADVISORY  WRITE 300 08:01:555 0 EOF\n"
             "3: -> FLOCK  ADVISORY  WRITE 100 08:01:555 0 EOF\n")

    def test_the_holder_is_the_process_outside_the_tree_with_the_write_lock(self):
        self.assertEqual(observe.lock_holders(self.LOCKS, {555}, inside={100}), {555: 300})

    def test_a_lock_the_tree_holds_itself_names_no_holder(self):
        self.assertEqual(observe.lock_holders(self.LOCKS, {777}, inside={200}), {})

    def test_waits_reads_the_kernel_wait_names_of_the_tree(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, wchan in ((100, "futex_wait_queue"), (101, "0")):
                os.makedirs(os.path.join(proc, str(pid), "task", str(pid)))
                with open(os.path.join(proc, str(pid), "task", str(pid), "wchan"), "w") as f:
                    f.write(wchan)
            table = [Proc(100, 1, 100, "claude", 0, 0, "/", False), Proc(101, 100, 100, "rustc", 0, 0, "/", False)]
            self.assertEqual(observe.waits(table, 100, proc), {"waiting_on": ["futex_wait_queue"], "lock_holder": ""})


class ShadowedSupervision(Supervised):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(config, "HOME", directory.name), mock.patch.object(config, "JEV_ACT", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.host.waits = lambda table, pid: {"waiting_on": ["futex_wait_queue"], "lock_holder": "cargo"}

    def ask(self, response):
        self.jev = chooser(response)
        self.host.shadow = lambda: self.jev

    def go_stalled(self, pid):
        self.watch()
        self.acts[self.wt] = Activity(1, 3, self.clock - 46 * MIN, self.clock - 46 * MIN, None)
        self.cpu(pid, 1.0)

    def logged(self):
        return jev.Log().read()

    def test_the_rule_still_stops_the_agent_while_jev_says_wait(self):
        pid = self.run_agent()
        self.ask(answer("wait"))
        self.go_stalled(pid)
        self.watch(46 * MIN)
        self.assertEqual(self.groups, [pid])
        record = self.logged()[-1]
        self.assertEqual((record["point"], record["rule"], record["jev"], record["acted"]), ("supervise.step", "nudge", "wait", "rule"))

    def test_the_wait_reason_and_lock_holder_reach_jev_and_no_path_does(self):
        pid = self.run_agent()
        self.ask(answer("wait"))
        self.go_stalled(pid)
        self.watch(46 * MIN)
        sent = self.jev.sent[-1]["state"]
        self.assertEqual((sent["waiting_on"], sent["lock_holder"], sent["verdict"]), (["futex_wait_queue"], "cargo", "STALLED"))
        self.assertNotIn(self.wt, json.dumps(self.jev.sent[-1]))

    def test_a_jev_that_cannot_be_reached_changes_nothing_the_rule_does(self):
        pid = self.run_agent()
        self.ask(jev.Transient("503"))
        self.go_stalled(pid)
        self.watch(46 * MIN)
        self.assertEqual(self.groups, [pid])
        self.assertIsNone(self.logged()[-1]["jev"])

    def test_with_shadowing_off_nothing_is_asked_or_logged(self):
        pid = self.run_agent()
        self.go_stalled(pid)
        self.watch(46 * MIN)
        self.assertEqual((self.groups, self.logged()), ([pid], []))

    def test_a_disallowed_confident_action_is_escalated_without_stopping_the_agent(self):
        with mock.patch.object(config, "JEV_ACT", True):
            log = jev.Log()
            for index in range(20):
                log.append({"id": f"prior-{index}", "point": "supervise.step", "rule": "wait", "jev": "wait",
                            "jev_outcome": "ok"})
            pid = self.run_agent()
            self.ask(answer("kill_and_hold", confidence=0.99))
            self.go_stalled(pid)
            self.watch(46 * MIN)
        self.assertEqual(self.groups, [])
        self.assertIn("jev:app-data", state.read()["needs_person"])

    def test_a_nudge_that_brings_a_commit_is_recorded_as_ok(self):
        pid = self.run_agent()
        self.ask(answer("nudge"))
        self.go_stalled(pid)
        self.watch(46 * MIN)
        self.assertIsNone(self.logged()[-1]["outcome"])
        second = self.agents()["app-data"]["pid"]
        self.live.add(second)
        self.table = [Proc(second, 1, second, "claude", 0, 9.0, "/", False)]
        self.acts[self.wt] = Activity(2, 3, self.clock + 5 * MIN, self.clock + 5 * MIN, None)
        self.watch(6 * MIN)
        self.assertEqual(self.logged()[0]["outcome"], "ok")

    def test_a_choice_with_no_work_after_it_is_recorded_as_wrong_after_the_stall_window(self):
        pid = self.run_agent()
        self.ask(answer("wait"))
        self.acts[self.wt] = Activity(1, 3, NOW - 7200, NOW - 7200, None)
        self.cpu(pid, 1.0)
        self.watch()
        self.cpu(pid, 5000.0)
        self.watch(44 * MIN)
        self.assertIsNone(self.logged()[0]["outcome"])
        self.cpu(pid, 9000.0)
        self.watch(1 * MIN)
        self.assertEqual(self.logged()[0]["outcome"], "wrong")


class ShadowedFixes(test_fix_agent.FixAgent):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patch in (mock.patch.object(config, "HOME", directory.name),
                      mock.patch.object(jev_points, "shadow_jev", lambda: chooser(answer("wait", ("start_fix", "wait", "tell_owner", "done", "stop"))))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_fix_agent_starts_whatever_jev_would_do_and_the_choice_is_logged(self):
        self.red_pr()
        self.world.tick()
        self.assertEqual(len(self.started.calls), 1)
        record = [r for r in jev.Log().read() if r["point"] == "fixes.decide"][0]
        self.assertEqual((record["rule"], record["jev"], record["state"]["has_attempt"]), ("start_fix", "wait", False))

    def test_the_outcome_is_ok_once_the_pr_is_no_longer_red(self):
        self.red_pr()
        self.world.tick()
        with state.change() as s:
            s["failed"].clear()
        pending = state.read()["jev_pending"]
        import nzfleet.tick as tick
        tick.settle_fix_decisions(pending, jev.Log(), {})
        self.assertEqual(jev.Log().read()[0]["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
