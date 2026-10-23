import unittest

from nzfleet import holds, plan
from nzfleet.plan import Pr

TODO = [{"key": "a", "task": "x"}, {"key": "b", "task": "y"}]
HOLD = {"a": {"why": "main is red", "since": 100.0}}


class After(unittest.TestCase):
    def test_a_pr_entry_is_met_only_once_the_pr_has_merged(self):
        self.assertFalse(holds.after_met(["pr:954"], set(), {954: "passed"}))
        self.assertFalse(holds.after_met(["pr:954"], set(), {}))
        self.assertTrue(holds.after_met(["pr:954"], set(), {954: "merged"}))

    def test_a_task_entry_is_met_once_the_task_is_done(self):
        self.assertFalse(holds.after_met(["a"], set(), {}))
        self.assertTrue(holds.after_met(["a"], {"a"}, {}))

    def test_every_entry_must_be_met(self):
        self.assertFalse(holds.after_met(["a", "pr:7"], {"a"}, {7: "waiting"}))
        self.assertTrue(holds.after_met(["a", "pr:7"], {"a"}, {7: "merged"}))

    def test_a_malformed_pr_entry_is_refused_with_the_form_to_use(self):
        self.assertIn("pr:<number>", holds.after_problem("pr:954a"))
        self.assertIn("pr:<number>", holds.after_problem("pr:"))
        self.assertEqual(holds.after_problem("pr:954"), "")
        self.assertEqual(holds.after_problem("some-task"), "")

    def test_the_prs_queued_tasks_wait_on_are_collected(self):
        todo = [{"key": "a", "after": ["pr:5", "x"]}, {"key": "b", "after": ["pr:9"]}, {"key": "c"}]
        self.assertEqual(holds.pending_prs(todo), {5, 9})


class Holding(unittest.TestCase):
    def test_a_hold_needs_a_reason(self):
        self.assertIn("--why", holds.hold_problem("core", "  ", TODO, {}, {}))

    def test_a_typo_does_not_hold_nothing(self):
        self.assertIn("neither a queued task, a running agent", holds.hold_problem("cor", "red", TODO, {}, {}))

    def test_a_task_or_a_group_can_be_held(self):
        self.assertEqual(holds.hold_problem("a", "red", TODO, {}, {}), "")
        self.assertEqual(holds.hold_problem("core", "red", TODO, {}, {}), "")

    def test_a_second_hold_on_one_target_is_refused(self):
        self.assertIn("main is red", holds.hold_problem("a", "again", TODO, HOLD, {}))

    def test_releasing_what_is_not_held_says_so(self):
        self.assertEqual(holds.release_problem("b", HOLD), "b is not held")
        self.assertEqual(holds.release_problem("a", HOLD), "")

    def test_a_release_removes_only_its_own_hold(self):
        both = holds.with_hold(HOLD, "core", " main is red ", 5.0)
        self.assertEqual(both["core"], {"why": "main is red", "since": 5.0})
        self.assertEqual(set(holds.without_hold(both, "core")), {"a"})

    def test_status_says_what_is_held_for_how_long_and_why(self):
        self.assertEqual(holds.hold_lines(HOLD, 100.0 + 600), ["a                        held 10 min: main is red"])


class Starting(unittest.TestCase):
    def test_a_held_task_is_skipped_for_the_next_one(self):
        self.assertEqual(plan.next_work(TODO, {}, 0, 4, False, {}, HOLD)["key"], "b")

    def test_a_released_task_starts_again(self):
        self.assertEqual(plan.next_work(TODO, {}, 0, 4, False, {}, holds.without_hold(HOLD, "a"))["key"], "a")

    def test_a_task_after_an_unmerged_pr_waits(self):
        todo = [{"key": "a", "task": "x", "after": ["pr:954"]}]
        self.assertIsNone(plan.next_work(todo, {}, 0, 4, False, {954: "passed"}))
        self.assertEqual(plan.next_work(todo, {}, 0, 4, False, {954: "merged"})["key"], "a")

    def test_a_hold_wins_over_an_after_that_has_been_met(self):
        todo = [{"key": "a", "task": "x", "area": "q", "after": ["pr:954"]}]
        self.assertIsNone(plan.next_work(todo, {}, 0, 4, False, {954: "merged"}, HOLD))

    def test_a_held_first_item_keeps_the_rest_of_its_area_waiting(self):
        todo = [{"key": "a", "task": "x", "area": "q"}, {"key": "b", "task": "y", "area": "q"}]
        self.assertIsNone(plan.next_work(todo, {}, 0, 4, False, {}, HOLD))


class Batching(unittest.TestCase):
    waiting = [Pr(1, "s1", frozenset({"rust"})), Pr(2, "s2", frozenset({"android"}))]

    def test_a_held_group_starts_no_batch_and_the_others_still_do(self):
        paused = set() | holds.held_groups({"core": {"why": "red", "since": 0}, "a": HOLD["a"]})
        self.assertEqual(paused, {"core"})
        started = plan.batches(self.waiting, {}, paused, 2)
        self.assertEqual([g for g, _, _ in started], ["android"])
