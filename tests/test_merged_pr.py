import unittest
from unittest import mock

from nzfleet import state, tick

from fakeworld import World, sh


def other(n):
    return f"crates/nzela/src/pr{n}.rs"


class MergedPrs(unittest.TestCase):
    def merged_but_moved_on(self, w):
        """PR 1 merged, then its branch was pushed to again: its head is no longer in main's history."""
        w.open_pr(1, {other(1): "one\n"})
        w.tick()
        w.merge_pr(1)
        sh(w.repo, "git", "checkout", "-q", "pr1")
        w.write(other(1), "one, again\n")
        w.commit("after the merge")
        w.push_branch(1, "pr1")
        sh(w.repo, "git", "checkout", "-q", "main")

    def chain(self):
        return {"agents": {"app-data": {"status": "done", "area": "app", "pr": {"number": 1, "branch": "pr1", "how": "stated"}}}}

    def test_a_pr_main_merged_is_merged_even_when_its_head_is_not_in_main(self):
        with World() as w:
            self.merged_but_moved_on(w)
            with state.change() as s:
                s["agents"] = self.chain()["agents"]
                s["todo"] = [{"key": "app-data", "area": "app"}]
            self.assertEqual(tick.standings(state.read()), {1: "merged"})

    def test_a_pr_main_has_not_merged_is_not_taken_for_merged(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            with state.change() as s:
                s["agents"] = self.chain()["agents"]
                s["todo"] = [{"key": "app-data", "area": "app"}]
            self.assertEqual(tick.standings(state.read()), {1: "waiting"})

    def test_the_time_main_merged_a_pr_is_recorded_once(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.merge_pr(1)
            first = w.tick()["merged_at"]["1"]
            w.advance_main({other(9): "x\n"})
            w.tick()
            self.assertEqual(state.read()["merged_at"]["1"], first)

    def test_a_merged_pr_retires_and_stops_its_running_rebase(self):
        with World() as w:
            with state.change() as s:
                s["todo"] = [{"key": "rebase-1", "task": "rebase"}]
                s["hands"] = {"1": {"sha": "head", "files": ["x"]}}
                s["agents"] = {"hands-1": {"status": "running", "pid": 445, "task": "rebase", "unit": "nzf-agent-hands-1-1"}}
            with mock.patch("nzfleet.tick.procs.alive", return_value=True), \
                    mock.patch("nzfleet.tick.os.getpgid", return_value=445), \
                    mock.patch("nzfleet.tick.procs.kill_group") as stop, \
                    mock.patch("nzfleet.tick.machine.stop_unit") as stop_unit:
                tick.retire_rebase_work({1})
            after = state.read()
        stop.assert_called_once_with(445)
        stop_unit.assert_called_once_with("nzf-agent-hands-1-1")
        self.assertEqual(after["todo"], [])
        self.assertEqual(after["hands"], {})
        self.assertEqual(after["agents"]["hands-1"]["status"], "retired")


if __name__ == "__main__":
    unittest.main()
