import unittest

from nzfleet import state, task_queue, tick

from fakeworld import World


def file(n):
    return f"crates/nzela/src/pr{n}.rs"


def build_train(w, members=(1, 2), number=9):
    """Opens `members`, then a train PR `number` carrying their changes under its own commits, as `nzfleet train` does."""
    for n in members:
        w.open_pr(n, {file(n): f"member {n}\n"})
    w.open_pr(number, {file(n): f"train copy of {n}\n" for n in members})
    with state.change() as s:
        s["train_prs"][str(number)] = {"branch": f"pr{number}", "members": list(members), "left_out": [], "dropped": [], "compiled": True}


def queue_behind(pr):
    """A finished task whose PR is `pr`, and the next task in its area, which waits on it."""
    with state.change() as s:
        s["agents"] = {"first": {"status": "done", "area": "app", "pr": {"number": pr, "branch": f"pr{pr}", "how": "stated"}}}
        s["todo"] = [{"key": "first", "area": "app", "task": "t"}, {"key": "second", "area": "app", "task": "t"}]


class TrainLanding(unittest.TestCase):
    def test_every_member_of_a_landed_train_is_recorded_as_merged(self):
        with World() as w:
            build_train(w)
            w.tick()
            w.merge_pr(9)
            merged_at = w.tick()["merged_at"]
            self.assertTrue({"1", "2"} <= set(merged_at))

    def test_members_are_closed_with_a_landed_in_comment(self):
        with World() as w:
            build_train(w)
            w.tick()
            w.merge_pr(9)
            w.tick()
            self.assertEqual(w.closed, {1: ["Landed in #9"], 2: ["Landed in #9"]})

    def test_a_train_is_settled_once(self):
        with World() as w:
            build_train(w)
            w.merge_pr(9)
            w.tick()
            first = state.read()["merged_at"]["1"]
            w.closed.clear()
            w.tick()
            self.assertEqual((w.closed, state.read()["merged_at"]["1"]), ({}, first))

    def test_a_queued_rebase_of_a_member_is_retired(self):
        with World() as w:
            build_train(w)
            w.open_pr(7, {"crates/nzela/src/seven.rs": "seven\n"})
            with state.change() as s:
                s["todo"] = [{"key": "rebase-2", "task": "t", "area": ""}, {"key": "rebase-7", "task": "t", "area": ""}]
                s["hands"] = {"1": {"sha": "x", "files": ["a"]}, "5": {"sha": "y", "files": ["b"]}}
            w.merge_pr(9)
            s = w.tick()
            self.assertEqual(([t["key"] for t in s["todo"]], {"1", "2"} & set(s["hands"])), (["rebase-7"], set()))

    def test_a_train_closed_without_merging_releases_its_members(self):
        with World() as w:
            build_train(w)
            w.tick()
            w.close_pr(9)
            s = {**w.tick(), "trains": {}}
            self.assertEqual(task_queue.in_flight_members(s), set())

    def test_a_train_still_open_keeps_its_members_in_flight(self):
        with World() as w:
            build_train(w)
            self.assertEqual(task_queue.in_flight_members({**w.tick(), "trains": {}}), {1, 2})

    def test_a_member_that_landed_in_a_train_is_retired_as_merged_not_closed(self):
        with World() as w:
            build_train(w, members=(1,))
            with state.change() as s:
                s["todo"] = [{"key": "rebase-1", "task": "t", "area": "", "kind": "rebase", "pr": 1}]
            w.merge_pr(9)
            w.close_pr(1)
            s = w.tick()
            self.assertEqual(s["task_history"]["rebase-1"]["why"], "rebase for #1: PR merged")


class LaneGate(unittest.TestCase):
    def standing(self, pr):
        return tick.standings(state.read())[pr]

    def test_a_pr_closed_with_a_landed_comment_counts_as_merged(self):
        with World() as w:
            w.open_pr(1, {file(1): "one\n"})
            queue_behind(1)
            w.close_pr(1, "Landed in #9")
            self.assertEqual(self.standing(1), "merged")

    def test_a_closed_pr_whose_change_is_on_main_counts_as_merged(self):
        with World() as w:
            w.open_pr(1, {file(1): "one\n"})
            queue_behind(1)
            w.close_pr(1)
            w.advance_main({file(1): "one\n"})
            self.assertEqual(self.standing(1), "merged")

    def test_a_closed_pr_that_never_landed_is_closed_not_waiting(self):
        with World() as w:
            w.open_pr(1, {file(1): "one\n"})
            queue_behind(1)
            w.close_pr(1)
            self.assertEqual(self.standing(1), "closed")

    def test_the_next_task_starts_once_its_predecessor_landed_in_a_train(self):
        with World() as w:
            build_train(w, members=(1,))
            queue_behind(1)
            w.close_pr(1, "Landed in #9")
            gate = tick.plan.area_gate(state.read()["todo"][1], state.read()["todo"], state.read()["agents"], tick.standings(state.read()))
            self.assertTrue(gate["go"])


if __name__ == "__main__":
    unittest.main()
