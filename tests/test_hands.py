import os
import tempfile
import unittest
from unittest import mock

from nzfleet import agents, config, holds, machine, plan, rebases, state, tick, world

NOW = 1_800_000_000.0
HANDS = {"5": {"sha": "h", "files": ["a.rs"], "parent": None}}


def pr(number, draft=False, mergeable="CONFLICTING", branch="nzf-branch"):
    return {"number": number, "isDraft": draft, "mergeable": mergeable, "headRefName": branch}


class WhichRebasesAreBarred(unittest.TestCase):
    def barred(self, prs, train_prs=None, in_flight=frozenset(), held=None):
        return rebases.barred(prs, train_prs or {}, set(in_flight), held or {})

    def test_a_conflicting_pr_is_not_barred(self):
        self.assertEqual(self.barred([pr(5)]), set())

    def test_a_draft_is_barred(self):
        self.assertEqual(self.barred([pr(5, draft=True)]), {5})

    def test_a_pr_that_already_merges_cleanly_is_barred(self):
        self.assertEqual(self.barred([pr(5, mergeable="MERGEABLE")]), {5})

    def test_a_held_rebase_is_barred(self):
        self.assertEqual(self.barred([pr(5)], held={"hands-5": {"why": "x", "since": 1.0}}), {5})

    def test_a_member_of_an_open_train_is_barred(self):
        self.assertEqual(self.barred([pr(5)], in_flight={5}), {5})

    def test_a_member_of_a_merged_train_is_barred(self):
        merged = {"900": {"members": [5], "landed": True}}
        self.assertEqual(self.barred([pr(5)], train_prs=merged), {5, 900})

    def test_a_train_pull_request_itself_is_barred(self):
        self.assertEqual(self.barred([pr(900)], train_prs={"900": {"members": []}}), {900})

    def test_a_pr_on_a_train_branch_is_barred_before_its_record_exists(self):
        self.assertEqual(self.barred([pr(900, branch="train-2026-10-15-S")]), {900})


class HoldingRebases(unittest.TestCase):
    def test_a_hands_rebase_can_be_held(self):
        self.assertEqual(holds.hold_problem("hands-1391", "why", [], {}, {}), "")

    def test_a_running_agent_can_be_held(self):
        self.assertEqual(holds.hold_problem("fix-7", "why", [], {}, {"fix-7": {"status": "running"}}), "")

    def test_a_resumed_attempt_is_held_under_its_run(self):
        self.assertEqual(holds.hold_problem("hands-1391~2", "why", [], {}, {}), "")

    def test_a_typo_still_holds_nothing(self):
        self.assertIn("neither", holds.hold_problem("hands-x", "why", [], {}, {}))


class Starting(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        for patch in (mock.patch.object(state, "PATH", os.path.join(home, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")),
                      mock.patch.object(state, "HOME", home),
                      mock.patch.object(tick, "tell_event"),
                      mock.patch.object(machine, "busy", return_value=False),
                      mock.patch.object(config, "AGENT_SLOTS", 2)):
            patch.start()
            self.addCleanup(patch.stop)

    def work(self, prs):
        with mock.patch.object(world, "open_prs", return_value=prs), \
                mock.patch.object(agents, "start", return_value="started") as start:
            tick.work(False)
        return [c.args[0] for c in start.call_args_list]

    def test_a_queued_now_item_starts_before_a_rebase(self):
        with state.change() as s:
            s["hands"] = HANDS
            s["todo"] = [{"key": "urgent", "task": "x", "now": True}]
        self.assertEqual(self.work([pr(5)]), ["urgent"])

    def test_a_rebase_starts_before_ordinary_queued_work(self):
        with state.change() as s:
            s["hands"] = HANDS
            s["todo"] = [{"key": "later", "task": "x"}]
        self.assertEqual(self.work([pr(5)]), ["hands-5"])

    def test_a_held_rebase_does_not_start(self):
        with state.change() as s:
            s["hands"] = HANDS
            s["holds"] = {"hands-5": {"why": "x", "since": NOW}}
        self.assertEqual(self.work([pr(5)]), [])

    def test_a_draft_rebase_does_not_start(self):
        with state.change() as s:
            s["hands"] = HANDS
        self.assertEqual(self.work([pr(5, draft=True)]), [])

    def test_a_rebase_of_a_merged_train_member_does_not_start(self):
        with state.change() as s:
            s["hands"] = HANDS
            s["train_prs"] = {"900": {"members": [5], "landed": True}}
        self.assertEqual(self.work([pr(5)]), [])

    def test_github_failure_still_bars_held_and_train_member_rebases(self):
        s = {
            "trains": {},
            "train_prs": {"900": {"members": [5], "landed": True}},
            "holds": {"hands-7": {"why": "owner", "since": NOW}},
        }
        with mock.patch.object(tick.state, "read", return_value=s), \
                mock.patch.object(world, "open_prs", side_effect=world.Failed("offline")):
            self.assertEqual(tick.barred_rebases(s), {5, 7, 900})

    def test_a_stopped_rebase_is_not_resumed_for_a_barred_pr(self):
        with state.change() as s:
            s["agents"] = {"hands-5": {"status": "unfinished", "finished": NOW - 10_000, "model": "sonnet",
                                       "task": "t", "for_pr": 5}}
        with mock.patch.object(world, "open_prs", return_value=[pr(5, draft=True)]), \
                mock.patch.object(agents, "start", return_value="started") as start:
            agents.resume(False, NOW, barred={5})
        start.assert_not_called()


class TrainBranches(unittest.TestCase):
    def test_shape_never_replays_a_train_branch_that_has_no_record_yet(self):
        home = tempfile.mkdtemp()
        with mock.patch.object(state, "PATH", os.path.join(home, "state.json")), \
                mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")), \
                mock.patch.object(state, "HOME", home), \
                mock.patch.object(world, "main_sha", return_value="m"), \
                mock.patch.object(tick.subprocess, "Popen") as popen:
            train = plan.Pr(900, "t", frozenset({"rust"}), False, False, True, "train-2026-10-15-S")
            tick.shape([train], {}, False)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ASiblingConflict(unittest.TestCase):
    setUp = Starting.setUp
    work = Starting.work

    def passed(self, *numbers):
        with state.change() as s:
            for n in numbers:
                s["batches"]["b"] = {"areas": [], "started": 1}
                s["passed"][str(n)] = {"sha": f"s{n}", "main": "m", "order": list(numbers), "batch": "b"}

    def ready(self, conflicting):
        prs = [plan.Pr(n, f"s{n}", frozenset()) for n in (1, 2)]
        with mock.patch.object(world, "merged_since", return_value=[]), \
                mock.patch.object(world, "merges_cleanly", return_value=conflicting), \
                mock.patch.object(world, "mark_ready"), \
                mock.patch.object(tick, "say") as said:
            return tick.ready(prs, dry_run=False), said

    def test_a_passed_pr_that_conflicts_with_one_ahead_of_it_is_named_and_queued_for_a_rebase(self):
        self.passed(1, 2)
        order, said = self.ready([2])
        self.assertEqual(order, [1])
        self.assertEqual(state.read()["hands"]["2"]["sibling"], True)
        self.assertIn("#2", said.call_args.args[0])

    def test_github_calling_it_mergeable_does_not_bar_that_rebase(self):
        self.passed(1, 2)
        self.ready([2])
        self.assertEqual(self.work([pr(2, mergeable="MERGEABLE")]), ["hands-2"])

    def test_it_is_named_once_for_one_head(self):
        self.passed(1, 2)
        self.ready([2])
        _, said = self.ready([2])
        said.assert_not_called()
