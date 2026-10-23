import unittest

from nzfleet import supervise
from nzfleet.progress import Verdict
from nzfleet.supervise import Proc, Step

WORK = "/w/work"


def proc(pid, ppid=1, name="bash", age=0, cpu=0.0, cwd="/", pgid=None, starved=False):
    return Proc(pid, ppid, pgid or pid, name, age, cpu, cwd, starved)


class ProcessTrees(unittest.TestCase):
    def test_a_trees_cpu_is_its_root_and_everything_below_it(self):
        procs = [proc(10, cpu=1.0), proc(11, ppid=10, cpu=2.0), proc(12, ppid=11, cpu=4.0), proc(13, ppid=99, cpu=100.0)]
        self.assertEqual(supervise.tree_cpu(procs, 10), 7.0)

    def test_a_root_that_is_gone_has_no_cpu(self):
        self.assertEqual(supervise.tree_cpu([proc(13)], 10), 0.0)


class Burners(unittest.TestCase):
    def stale(self, **kw):
        return proc(20, ppid=11, name="yes", age=11 * 60, cwd=f"{WORK}/app", **kw)

    def test_a_yes_loop_an_agent_started_is_stopped_after_ten_minutes(self):
        procs = [proc(10), proc(11, ppid=10), self.stale()]
        self.assertEqual([p.pid for p in supervise.burners(procs, {10}, WORK)], [20])

    def test_a_burner_under_ten_minutes_is_left_alone(self):
        young = proc(20, ppid=11, name="yes", age=9 * 60, cwd=f"{WORK}/app")
        self.assertEqual(supervise.burners([proc(10), proc(11, ppid=10), young], {10}, WORK), [])

    def test_a_burner_run_by_nzfleet_starve_is_never_stopped(self):
        self.assertEqual(supervise.burners([proc(10), proc(11, ppid=10), self.stale(starved=True)], {10}, WORK), [])

    def test_a_yes_that_belongs_to_no_agent_and_no_worktree_is_not_ours_to_stop(self):
        foreign = proc(20, ppid=1, name="yes", age=3600, cwd="/home/someone")
        self.assertEqual(supervise.burners([foreign], {10}, WORK), [])

    def test_a_burner_in_a_worktree_counts_even_when_its_parent_is_gone(self):
        orphan = proc(20, ppid=1, name="yes", age=3600, cwd=f"{WORK}/app")
        self.assertEqual([p.pid for p in supervise.burners([orphan], set(), WORK)], [20])

    def test_other_long_running_programs_are_not_burners(self):
        cargo = proc(20, ppid=11, name="cargo", age=3600, cwd=f"{WORK}/app")
        self.assertEqual(supervise.burners([proc(10), proc(11, ppid=10), cargo], {10}, WORK), [])

    def test_a_worktree_prefix_is_not_the_worktree(self):
        near = proc(20, ppid=1, name="yes", age=3600, cwd="/w/work-other/app")
        self.assertEqual(supervise.burners([near], set(), WORK), [])


class DuplicateBatches(unittest.TestCase):
    def running(self, members, started, pid):
        return {"status": "running", "members": [[n, "sha"] for n in members], "started": started, "pid": pid, "group": "core"}

    def test_two_running_batches_for_one_set_of_prs_stop_the_newer(self):
        batches = {"a": self.running([1, 2], 100, 11), "b": self.running([2, 1], 200, 12)}
        self.assertEqual(supervise.duplicate_batches(batches, {11, 12}), [("b", 12)])

    def test_different_sets_are_not_duplicates(self):
        batches = {"a": self.running([1], 100, 11), "b": self.running([1, 2], 200, 12)}
        self.assertEqual(supervise.duplicate_batches(batches, {11, 12}), [])

    def test_a_batch_that_already_ended_is_not_counted(self):
        batches = {"a": {**self.running([1], 100, 11), "status": "green"}, "b": self.running([1], 200, 12)}
        self.assertEqual(supervise.duplicate_batches(batches, {11, 12}), [])

    def test_a_batch_whose_process_is_gone_is_left_to_the_tick_to_mark_lost(self):
        batches = {"a": self.running([1], 100, 11), "b": self.running([1], 200, 12)}
        self.assertEqual(supervise.duplicate_batches(batches, {12}), [])


class Judging(unittest.TestCase):
    def test_a_hung_agent_is_resumed(self):
        self.assertEqual(supervise.step(Verdict.HUNG, nudged_before=False), Step.RESUME)

    def test_a_stalled_agent_is_nudged_once(self):
        self.assertEqual(supervise.step(Verdict.STALLED, nudged_before=False), Step.NUDGE)

    def test_a_stalled_agent_that_was_nudged_already_goes_to_the_owner(self):
        self.assertEqual(supervise.step(Verdict.STALLED, nudged_before=True), Step.EMAIL)

    def test_a_moving_agent_is_left_alone(self):
        self.assertEqual(supervise.step(Verdict.MOVING, nudged_before=True), Step.NONE)


class IdleChains(unittest.TestCase):
    def setUp(self):
        self.todo = [{"key": "one", "area": "app"}, {"key": "two", "area": "app"}]

    def idle(self, agents, standing=None, holds=None, running=0, slots=4, busy=False):
        return supervise.idle_chains(self.todo, agents, standing or {}, holds or {}, running, slots, busy, {})

    def test_a_chain_waiting_on_an_unmerged_pr_says_which(self):
        agents = {"one": {"status": "done", "pr": {"number": 7, "branch": "b", "how": "stated"}}}
        (idle,) = self.idle(agents, {7: "waiting"})
        self.assertEqual((idle.area, idle.key), ("app", "two"))
        self.assertIn("#7", idle.why)

    def test_a_chain_whose_head_could_start_but_has_not_says_so_as_a_fault(self):
        (idle,) = supervise.idle_chains([{"key": "one", "area": "app"}], {}, {}, {}, 0, 4, False, {})
        self.assertIn("could start", idle.why)

    def test_a_chain_is_not_idle_when_the_machine_is_busy(self):
        self.assertEqual(self.idle({}, busy=True), [])

    def test_a_chain_is_not_idle_when_all_agent_slots_are_taken(self):
        self.assertEqual(self.idle({}, running=4), [])

    def test_a_held_head_is_not_idle(self):
        self.assertEqual(self.idle({}, holds={"one": {"why": "x", "since": 0}}), [])

    def test_a_chain_with_a_run_in_progress_is_not_idle(self):
        self.assertEqual(self.idle({"one": {"status": "running"}}, running=1), [])

    def test_a_pr_none_with_no_change_is_named_as_the_known_false_block(self):
        agents = {"one": {"status": "done", "pr": None, "stated_no_pr": True}}
        (idle,) = self.idle(agents)
        self.assertEqual(idle.no_pr_of, "one")

    def test_a_chain_waiting_on_a_resumption_is_not_idle(self):
        agents = {"one": {"status": "unfinished", "finished": 0}}
        self.assertEqual(self.idle(agents), [])


if __name__ == "__main__":
    unittest.main()
