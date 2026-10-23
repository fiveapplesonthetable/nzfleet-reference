import json
import unittest
from unittest import mock

from nzfleet import agents, dispatch, machine, state, task_queue, tick, world

from fleet import Fleet, NOW

HEAD = "a" * 40
OPEN = dispatch.PullFacts("OPEN", HEAD, "train-1", True, True)
CLOSED = dispatch.PullFacts("CLOSED", HEAD, "train-1", True, True)


def train_fix(pr, area="rust"):
    return {"key": f"train-fix-{pr}-{area}", "task": "fix it", "model": "sonnet", "area": "", "after": [], "kind": "train-fix", "pr": pr}


class QueuedWork(Fleet):
    def setUp(self):
        super().setUp()
        self.facts = {}
        for patch in (mock.patch.object(world, "pull_facts", side_effect=lambda n: self.facts[n]),
                      mock.patch.object(world, "open_prs", return_value=[]),
                      mock.patch.object(machine, "busy", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def queue(self, *items):
        with state.change() as s:
            s["todo"] = list(items)

    def test_a_task_for_a_closed_pr_is_retired_with_its_reason(self):
        self.facts = {1400: CLOSED}
        self.queue(train_fix(1400))
        tick.work(False)
        after = state.read()
        self.assertEqual(after["todo"], [])
        self.assertEqual(after["task_history"]["train-fix-1400-rust"]["status"], "retired")
        self.assertIn("#1400 is closed", after["task_history"]["train-fix-1400-rust"]["why"])
        self.assertEqual(self.launched, [])

    def test_a_refused_task_does_not_hold_up_the_one_behind_it(self):
        self.facts = {1400: CLOSED, 1401: OPEN}
        self.queue(train_fix(1400), train_fix(1401))
        tick.work(False)
        tick.work(False)
        self.assertIn("train-fix-1401-rust", state.read()["agents"])
        self.assertEqual(state.read()["agents"]["train-fix-1401-rust"]["for_pr"], 1401)

    def test_a_task_for_an_open_pr_starts_on_that_pr(self):
        self.facts = {1400: OPEN}
        self.queue(train_fix(1400))
        tick.work(False)
        self.assertEqual(state.read()["agents"]["train-fix-1400-rust"]["for_pr"], 1400)

    def test_a_task_that_names_no_pr_starts_without_a_check(self):
        self.queue({"key": "plain", "task": "do", "model": "sonnet", "area": "", "after": [], "kind": "task", "pr": None})
        tick.work(False)
        self.assertIn("plain", state.read()["agents"])

    def test_a_rebase_of_a_member_of_a_native_train_is_not_handed_out(self):
        with state.change() as s:
            s["hands"] = {"7": {"sha": "x", "files": ["f"]}}
            s["train_prs"] = {"900": {"members": [7], "source_heads": {"7": "x"}}}
        with mock.patch.object(agents, "start") as start:
            tick.work(False)
        start.assert_not_called()

    def test_a_resumed_run_of_a_native_train_member_is_not_resumed(self):
        with state.change() as s:
            s["train_prs"] = {"900": {"members": [7], "source_heads": {"7": "x"}}}
            s["agents"] = {"hands-7~2": {"status": "unfinished", "finished": NOW, "model": "sonnet", "task": "t", "for_pr": 7}}
        with mock.patch.object(agents, "start") as start:
            agents.resume(now=NOW + 10 ** 6)
        start.assert_not_called()


class InFlightMembers(unittest.TestCase):
    def test_open_trains_and_unlanded_train_prs_are_both_in_flight(self):
        s = {"trains": {"t": {"status": "open", "members": [[3, "c"]]}},
             "train_prs": {"900": {"members": [7], "source_heads": {"7": "x"}}, "901": {"members": [8], "landed": True, "source_heads": {"8": "y"}}}}
        self.assertEqual(task_queue.in_flight_members(s), {3, 7})


class OldState(Fleet):
    def written_before_items_were_typed(self, *keys):
        with open(state.PATH, "w") as output:
            json.dump({"todo": [{"key": k, "task": "t", "model": "sonnet", "area": "", "after": []} for k in keys]}, output)
        return {t["key"]: t for t in state.read()["todo"]}

    def test_an_old_train_fix_reads_with_its_train_pr(self):
        item = self.written_before_items_were_typed("train-fix-1400-rust")["train-fix-1400-rust"]
        self.assertEqual((item["kind"], item["pr"]), ("train-fix", 1400))

    def test_an_old_rebase_and_compile_fix_read_with_the_pr_they_work_on(self):
        items = self.written_before_items_were_typed("rebase-77", "fix-compile-78")
        self.assertEqual([(i["kind"], i["pr"]) for i in items.values()], [("rebase", 77), ("fix-compile", 78)])

    def test_any_other_old_item_reads_as_a_task_with_no_pr(self):
        item = self.written_before_items_were_typed("app-data")["app-data"]
        self.assertEqual((item["kind"], item["pr"]), ("task", None))

    def test_an_item_that_is_already_typed_is_left_alone(self):
        with state.change() as s:
            s["todo"] = [{"key": "rebase-5", "task": "t", "kind": "task", "pr": None}]
        self.assertEqual(state.read()["todo"][0]["kind"], "task")


class StalePrs(Fleet):
    def test_pruning_retires_a_task_whose_pr_is_no_longer_open(self):
        with state.change() as s:
            s["todo"] = [train_fix(1400), train_fix(1401)]
        tick.prune_stale([{"number": 1401, "headRefOid": "h"}], {1400})
        after = state.read()
        self.assertEqual([t["key"] for t in after["todo"]], ["train-fix-1401-rust"])
        self.assertEqual(after["task_history"]["train-fix-1400-rust"]["why"], "train-fix for #1400: PR merged")


if __name__ == "__main__":
    unittest.main()
