import unittest

from nzfleet import progress
from nzfleet.progress import Activity, Sample, Verdict

NOW = 1_800_000_000.0
MIN = 60
HOUR = 3600


def activity(commit=None, change=None, test=None, commits=0, files=0):
    return Activity(commits, files, None if commit is None else NOW - commit, None if change is None else NOW - change,
                    None if test is None else NOW - test)


def sample(alive=0, busy=0, cpu=600.0):
    """A process tree last seen growing `alive` seconds ago and last busy `busy` seconds ago."""
    return Sample(cpu, NOW, NOW - alive, NOW - busy)


def judge(act, smp, started=NOW - 5 * HOUR):
    return progress.verdict(act, smp, started, NOW)


class Stalling(unittest.TestCase):
    def test_an_agent_that_changed_a_file_recently_is_moving_however_idle_its_cpu_is(self):
        self.assertEqual(judge(activity(change=10 * MIN), sample(alive=0, busy=3 * HOUR)), Verdict.MOVING)

    def test_an_agent_that_committed_recently_is_moving(self):
        self.assertEqual(judge(activity(commit=44 * MIN, change=3 * HOUR), sample(busy=3 * HOUR)), Verdict.MOVING)

    def test_no_change_for_45_minutes_with_an_idle_cpu_is_stalled(self):
        self.assertEqual(judge(activity(commit=2 * HOUR, change=46 * MIN), sample(alive=0, busy=50 * MIN)), Verdict.STALLED)

    def test_no_change_for_45_minutes_while_the_cpu_is_busy_is_still_moving(self):
        self.assertEqual(judge(activity(change=46 * MIN), sample(busy=5 * MIN)), Verdict.MOVING)

    def test_a_process_tree_whose_cpu_has_not_moved_at_all_for_30_minutes_with_no_file_change_is_hung(self):
        self.assertEqual(judge(activity(change=31 * MIN), sample(alive=31 * MIN, busy=31 * MIN)), Verdict.HUNG)

    def test_a_quiet_but_breathing_process_is_not_hung_at_30_minutes(self):
        self.assertEqual(judge(activity(change=31 * MIN), sample(alive=1 * MIN, busy=31 * MIN)), Verdict.MOVING)

    def test_an_agent_that_never_touched_its_worktree_is_judged_from_when_it_started(self):
        self.assertEqual(judge(activity(), sample(alive=1 * MIN, busy=60 * MIN), started=NOW - 50 * MIN), Verdict.STALLED)
        self.assertEqual(judge(activity(), sample(alive=1 * MIN, busy=60 * MIN), started=NOW - 10 * MIN), Verdict.MOVING)


class Sampling(unittest.TestCase):
    def test_the_first_sight_of_a_process_counts_as_alive_and_busy(self):
        first = progress.take_sample(None, 5.0, NOW)
        self.assertEqual((first.alive_at, first.busy_at), (NOW, NOW))

    def test_a_cpu_that_grew_fast_is_busy_and_alive(self):
        later = progress.take_sample(Sample(10.0, NOW, NOW - 900, NOW - 900), 40.0, NOW + 60)
        self.assertEqual((later.alive_at, later.busy_at), (NOW + 60, NOW + 60))

    def test_a_cpu_that_grew_a_little_is_alive_but_not_busy(self):
        later = progress.take_sample(Sample(10.0, NOW, NOW - 900, NOW - 900), 10.5, NOW + 60)
        self.assertEqual((later.alive_at, later.busy_at), (NOW + 60, NOW - 900))

    def test_a_cpu_that_did_not_grow_is_neither(self):
        later = progress.take_sample(Sample(10.0, NOW, NOW - 900, NOW - 900), 10.0, NOW + 60)
        self.assertEqual((later.alive_at, later.busy_at), (NOW - 900, NOW - 900))


class Phases(unittest.TestCase):
    def notes(self, *rows):
        return [{"at": NOW - ago, "phase": phase, "line": line} for ago, phase, line in rows]

    def test_the_phase_is_the_latest_note_and_runs_from_its_first_consecutive_note(self):
        notes = self.notes((5 * HOUR, "read", "the code"), (3 * HOUR, "write", "the store"), (2 * HOUR - 10, "write", "more"))
        cur = progress.current_phase(notes, NOW)
        self.assertEqual((cur.phase, cur.since, cur.line), ("write", NOW - 3 * HOUR, "more"))

    def test_a_phase_over_two_hours_is_flagged_with_its_last_note(self):
        cur = progress.current_phase(self.notes((3 * HOUR, "test", "waiting on the console suite")), NOW)
        self.assertTrue(progress.phase_is_long(cur, NOW))
        self.assertEqual(cur.line, "waiting on the console suite")

    def test_a_phase_under_two_hours_is_not_flagged(self):
        self.assertFalse(progress.phase_is_long(progress.current_phase(self.notes((HOUR, "fix", "x")), NOW), NOW))

    def test_an_agent_that_never_noted_has_no_phase(self):
        self.assertIsNone(progress.current_phase([], NOW))

    def test_a_note_is_a_known_phase_and_one_line(self):
        self.assertEqual(progress.parse_note("test: running the store tests").phase, "test")
        for bad in ("dance: x", "no colon", "test:", "write: " + "x" * 300):
            self.assertIsNone(progress.parse_note(bad), bad)


def agent(started, finished=None, pr=None, status="done"):
    run = {"status": status, "started": NOW - started, "pr": pr and {"number": pr, "branch": "b", "how": "stated"}}
    if finished is not None:
        run["finished"] = NOW - finished
    return run


def batch(started, finished):
    return {"started": NOW - started, "finished": NOW - finished, "status": "green"}


class Chains(unittest.TestCase):
    """Two steps in the `app` chain: the first ran, passed a batch and merged; the second is running."""

    def setUp(self):
        self.todo = [{"key": "one", "area": "app"}, {"key": "two", "area": "app"}, {"key": "loose"}]
        self.agents = {"one": agent(10 * HOUR, 9 * HOUR, pr=7), "two": agent(2 * HOUR, status="running")}
        self.batches = {"b1": batch(8 * HOUR + 30 * MIN, 8 * HOUR)}
        self.passed = {"7": {"batch": "b1"}}
        self.merged_at = {"7": NOW - 5 * HOUR}
        self.standing = {7: "merged"}

    def rows(self):
        return {r.area: r for r in progress.chain_rows(self.todo, self.agents, self.batches, self.passed, self.merged_at, self.standing, NOW)}

    def test_a_step_is_an_agent_run_a_batch_or_the_wait_to_merge_and_each_has_a_median(self):
        row = self.rows()["app"]
        self.assertEqual((row.run_median, row.batch_median, row.ready_median), (1 * HOUR, 30 * MIN, 3 * HOUR))

    def test_the_wait_on_the_owner_and_the_wait_on_the_machine_are_told_apart(self):
        row = self.rows()["app"]
        self.assertEqual(row.owner_wait, 3 * HOUR)      # batch passed 8 h ago, merged 5 h ago
        self.assertEqual(row.machine_wait, 30 * MIN)    # run ended 9 h ago, batch began 8.5 h ago

    def test_a_pr_that_passed_and_is_still_unmerged_adds_its_wait_so_far_to_the_owner(self):
        self.agents["two"] = agent(2 * HOUR, 1 * HOUR, pr=8)
        self.passed["8"] = {"batch": "b2"}
        self.batches["b2"] = batch(50 * MIN, 40 * MIN)
        self.standing[8] = "passed"
        self.assertEqual(self.rows()["app"].owner_wait, 3 * HOUR + 40 * MIN)

    def test_steps_are_counted_by_when_they_ended(self):
        row = self.rows()["app"]
        self.assertEqual(row.steps_day, 3)    # run ended 9 h ago, batch 8 h ago, merge 5 h ago
        self.assertEqual(row.steps_hour, 0)

    def test_a_step_that_ended_within_the_hour_counts_in_the_hour(self):
        self.agents["two"] = agent(2 * HOUR, 10 * MIN, pr=None)
        self.assertEqual(self.rows()["app"].steps_hour, 1)

    def test_the_head_is_the_first_step_not_done_and_merged_and_its_time_there_runs_from_the_merge_before_it(self):
        row = self.rows()["app"]
        self.assertEqual((row.head, row.head_for), ("two", 5 * HOUR))

    def test_a_head_that_waits_to_be_merged_is_still_the_head(self):
        self.agents["two"] = agent(2 * HOUR, 1 * HOUR, pr=8)
        self.standing[8] = "passed"
        self.assertEqual(self.rows()["app"].head, "two")

    def test_a_chain_with_everything_merged_has_no_head(self):
        self.agents["two"] = agent(2 * HOUR, 1 * HOUR, pr=8)
        self.standing[8] = "merged"
        self.assertIsNone(self.rows()["app"].head)

    def test_items_with_no_area_are_not_a_chain(self):
        self.assertEqual(set(self.rows()), {"app"})

    def test_a_chain_with_no_finished_step_has_no_medians_rather_than_zeros(self):
        self.agents = {"one": agent(HOUR, status="running")}
        self.batches, self.passed, self.merged_at, self.standing = {}, {}, {}, {}
        row = self.rows()["app"]
        self.assertEqual((row.run_median, row.batch_median, row.ready_median, row.steps_day), (None, None, None, 0))


class Rendering(unittest.TestCase):
    def test_the_health_line_says_which_agents_and_chains_moved_in_the_last_hour(self):
        agents = [progress.AgentRow("one", "moving", 5 * MIN, 1 * MIN, 20 * MIN, 3, 12, 41.0, "test", 8 * MIN, "x", False),
                  progress.AgentRow("two", "STALLED", 3 * HOUR, 50 * MIN, None, 1, 36, 5.0, "write", 3 * HOUR, "y", True)]
        chains = [progress.ChainRow("app", 2, 9, 3600, 600, 1200, 0, 0, "three", 7200)]
        line = progress.health_line(agents, chains)
        for part in ("one moving", "two STALLED", "app 2 steps in the last hour"):
            self.assertIn(part, line)
        self.assertNotIn("\n", line)

    def test_the_table_has_a_row_per_agent_and_chain(self):
        agents = [progress.AgentRow("one", "moving", 5 * MIN, 1 * MIN, None, 3, 12, 41.0, "test", 8 * MIN, "running tests", False)]
        chains = [progress.ChainRow("app", 2, 9, 3600, 600, 1200, 0, 0, "three", 7200)]
        text = progress.table(agents, chains)
        for part in ("one", "moving", "5 min", "test", "running tests", "app", "three", "2 h"):
            self.assertIn(part, text)

    def test_a_long_phase_is_marked_with_its_last_note(self):
        row = progress.AgentRow("two", "moving", None, None, None, 0, 0, 0.0, "test", 3 * HOUR, "waiting on the suite", True)
        self.assertIn("over 2 h in test: waiting on the suite", progress.table([row], []))


if __name__ == "__main__":
    unittest.main()
