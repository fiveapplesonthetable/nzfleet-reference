import unittest
from unittest import mock

from nzfleet import attention, dispatch, machine, plan, state, supervise, tick, world

from fleet import Fleet

CLOSED_PR = {"number": 7, "branch": "first-branch", "how": "stated"}
DONE = {"status": "done", "pr": CLOSED_PR}
TODO = [{"key": "first", "area": "app"}, {"key": "second", "area": "app", "task": "go", "model": "sonnet"}]


class AnAreaBehindAClosedPr(unittest.TestCase):
    def test_starts_on_main_and_names_the_pr_it_skipped(self):
        gate = plan.area_gate(TODO[1], TODO, {"first": DONE}, {7: "closed"})
        self.assertEqual(gate, {"go": True, "base": {"on": "main"}, "skipped": 7})

    def test_still_waits_while_the_pr_is_open(self):
        self.assertFalse(plan.area_gate(TODO[1], TODO, {"first": DONE}, {7: "waiting"})["go"])

    def test_is_reported_ready_by_the_area_report(self):
        report = plan.area_report(TODO, {"first": DONE}, {7: "closed"}, {})
        self.assertEqual(report["app"][1], "second: queued, ready to start")

    def test_still_has_its_gate_after_the_head_was_compacted_away(self):
        todo = TODO[1:]
        heads = {"app": "first"}
        self.assertFalse(plan.area_gate(todo[0], todo, {"first": DONE}, {7: "waiting"}, heads)["go"])
        self.assertTrue(plan.area_gate(todo[0], todo, {"first": DONE}, {7: "closed"}, heads)["go"])

    def test_is_not_called_idle_for_waiting_on_a_head_that_compaction_removed(self):
        agents = {"first": DONE}
        idle = supervise.idle_chains(TODO[1:], agents, {7: "waiting"}, {}, 0, 4, False, {"app": "first"})
        self.assertIn("#7", idle[0].why)


class AQueuedItemAfterAClosedPr(Fleet):
    def setUp(self):
        super().setUp()
        self.facts = {7: dispatch.PullFacts("CLOSED", "a" * 40, "b", True, True)}
        for patch in (mock.patch.object(world, "pull_facts", side_effect=lambda n: self.facts[n]),
                      mock.patch.object(machine, "busy", return_value=False),
                      mock.patch.object(tick, "barred_rebases", return_value=set())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_is_retired_with_the_reason(self):
        with state.change() as s:
            s["todo"] = [{"key": "after-seven", "task": "x", "model": "sonnet", "area": "", "after": ["pr:7"]}]
        with mock.patch.object(tick, "standings", return_value={7: "closed"}):
            tick.work(False)
        after = state.read()
        self.assertEqual(after["todo"], [])
        self.assertIn("#7 was closed without landing", after["task_history"]["after-seven"]["why"])
        self.assertEqual(self.launched, [])

    def test_in_an_area_starts_on_main_and_tells_the_owner_once(self):
        with state.change() as s:
            s["todo"] = [dict(TODO[1], after=[])]
            s["agents"] = {"first": {**DONE, "area": "app"}}
            s["area_heads"] = {"app": "first"}
        with mock.patch.object(tick, "standings", return_value={7: "closed"}):
            tick.work(False)
            tick.work(False)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(len([m for m in self.said if "closed without landing" in m]), 1)

    def test_old_state_with_closed_waits_still_reads(self):
        with state.change() as s:
            s["closed_waits"] = {"second": {"pr": 7, "since": 0.0}}
        self.assertEqual(attention.needs_you(state.read()), [])


if __name__ == "__main__":
    unittest.main()
