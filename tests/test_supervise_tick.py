import os
import unittest
from unittest import mock

from nzfleet import agents, owner_tick, progress, runs, state, supervise_tick, tick, train
from nzfleet.progress import Activity
from nzfleet.supervise import Proc
from nzfleet.supervise_tick import Host

from fleet import Fleet, NOW

MIN = 60
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-u16085.scope"
QUIET = Activity(0, 0, None, None, None)


class Supervised(Fleet):
    """An agent running in a real worktree, on a machine whose clock, processes and worktree activity the test sets."""

    def setUp(self):
        super().setUp()
        self.clock, self.table, self.acts = NOW, [], {}
        self.live, self.killed, self.groups, self.told, self.events = set(), [], [], [], []
        self.comments = []
        self.host = Host(now=lambda: self.clock, processes=lambda: self.table, activity=lambda path, base: self.acts.get(path, QUIET),
                         alive=lambda pid: pid in self.live, kill=self.killed.append, kill_group=self.groups.append,
                         comment=lambda n, body: self.comments.append((n, body)), busy=lambda: False, load=lambda: 5.0, cpu_percent=lambda: 40.0, free_gb=lambda: 200, root_usage=lambda: (10, 200))

    def run_agent(self, key="app-data", cpu=0.0):
        self.start(key)
        pid = self.agents()[key]["pid"]
        self.live.add(pid)
        self.table = [Proc(pid, 1, pid, "claude", 0, cpu, "/", False)]
        return pid

    def cpu(self, pid, seconds):
        self.table = [Proc(pid, 1, pid, "claude", 0, seconds, "/", False)]

    def watch(self, advance=0):
        self.clock += advance
        supervise_tick.watch_agents(self.host, self.told.append)

    def stop_burners(self):
        supervise_tick.stop_burners(self.host, self.told.append)

    def hourly(self, standing=None):
        return supervise_tick.hourly(self.host, lambda s: standing or {}, self.told.append, self.events.append)


class Stalls(Supervised):
    def go_quiet(self, pid, minutes):
        """Nothing written for `minutes`, the CPU creeping along."""
        self.acts[self.wt] = Activity(1, 3, self.clock - minutes * MIN, self.clock - minutes * MIN, None)
        self.cpu(pid, 1.0)

    def test_an_agent_that_keeps_writing_is_left_alone(self):
        pid = self.run_agent()
        self.acts[self.wt] = Activity(1, 3, NOW, NOW - 2 * MIN, None)
        self.watch(60 * MIN)
        self.assertEqual((self.groups, self.told), ([], []))

    def test_a_stalled_agent_is_stopped_and_resumed_at_once_with_the_question(self):
        pid = self.run_agent()
        self.watch()
        self.go_quiet(pid, 46)
        self.watch(46 * MIN)
        self.assertEqual(self.groups, [pid])
        self.assertEqual(self.agents()["app-data"]["status"], "unfinished")
        agents.resume(now=self.clock)
        self.assertIn("what is blocking", self.agents()["app-data~2"]["task"])
        self.assertIn(self.wt, self.agents()["app-data~2"]["task"])

    def test_a_stalled_agent_that_was_nudged_goes_to_the_owner_instead_of_being_stopped_again(self):
        pid = self.run_agent()
        self.watch()
        self.go_quiet(pid, 46)
        self.watch(46 * MIN)
        agents.resume(now=self.clock)
        second = self.agents()["app-data~2"]["pid"]
        self.live.add(second)
        self.table = [Proc(second, 1, second, "claude", 0, 1.0, "/", False)]
        self.groups.clear()
        with state.change() as s:
            s["notes"]["app-data"] = [{"at": NOW, "phase": "test", "line": "waiting on the console suite"}]
        self.watch()
        self.clock += 50 * MIN
        self.acts[self.wt] = Activity(1, 3, NOW - 3600, NOW - 3600, None)
        self.cpu(second, 1.5)
        self.watch()
        self.assertEqual(self.groups, [])
        with mock.patch.object(tick.time, "time", return_value=self.clock):
            tick.owner_loop(False)
            tick.owner_loop(False)
        (subject, body), = self.mails
        for part in ("app-data~2", self.wt, "waiting on the console suite", "nudged"):
            self.assertIn(part, body)

    def test_an_agent_that_moves_again_clears_the_need_for_a_person(self):
        pid = self.run_agent()
        with state.change() as s:
            s["needs_person"]["stalled-app-data"] = "x"
        self.acts[self.wt] = Activity(1, 3, NOW, NOW, None)
        self.watch()
        self.assertEqual(state.read()["needs_person"], {})

    def test_a_busy_cpu_keeps_a_quiet_agent_moving(self):
        pid = self.run_agent()
        self.watch()
        self.acts[self.wt] = Activity(1, 3, NOW - 3600, NOW - 3600, None)
        self.cpu(pid, 600.0)
        self.watch(60 * MIN)
        self.assertEqual(self.groups, [])

    def test_a_hung_agent_is_stopped_and_resumed_after_the_backoff_without_the_question(self):
        pid = self.run_agent()
        self.acts[self.wt] = Activity(1, 3, NOW, NOW, None)
        self.watch()
        self.watch(31 * MIN)
        self.assertEqual(self.groups, [pid])
        agents.resume(now=self.clock)
        self.assertNotIn("app-data~2", self.agents())
        agents.resume(now=self.clock + runs.BACKOFF[0])
        self.assertNotIn("what is blocking", self.agents()["app-data~2"]["task"])
        self.assertIn(self.wt, self.agents()["app-data~2"]["task"])

    def test_a_process_that_does_not_lead_its_own_group_is_never_killed(self):
        pid = self.run_agent()
        self.table = [Proc(pid, 1, 99, "claude", 0, 0.0, "/", False)]
        self.watch()
        self.watch(31 * MIN)
        self.assertEqual(self.groups, [])

    def test_an_agent_whose_process_is_gone_is_left_to_the_reaper(self):
        pid = self.run_agent()
        self.live.clear()
        self.watch(3 * 3600)
        self.assertEqual(self.groups, [])

    def test_the_cpu_of_the_whole_process_tree_is_what_counts(self):
        pid = self.run_agent()
        self.watch()
        self.table = [Proc(pid, 1, pid, "claude", 0, 0.0, "/", False), Proc(pid + 1, pid, pid, "cargo", 0, 900.0, "/", False)]
        self.acts[self.wt] = Activity(1, 3, NOW - 3600, NOW - 3600, None)
        self.watch(60 * MIN)
        self.assertEqual(self.groups, [])

    def test_the_sample_is_kept_for_the_next_tick(self):
        pid = self.run_agent()
        self.cpu(pid, 12.0)
        self.watch()
        self.assertEqual(state.read()["samples"]["app-data"]["cpu"], 12.0)


class Hourly(Supervised):
    def burner(self, pid=900, ppid=1, age=11 * MIN, starved=False, cwd=None):
        self.table.append(Proc(pid, ppid, pid, "yes", age, 600.0, cwd or self.wt, starved))

    def test_a_burner_left_over_ten_minutes_is_stopped_and_the_agent_is_told_in_its_worktree(self):
        pid = self.run_agent()
        self.burner(ppid=pid)
        self.stop_burners()
        self.assertEqual(self.killed, [900])
        notice = open(os.path.join(self.wt, ".nzrun", "NZFLEET-NOTICE.txt")).read()
        self.assertIn("nzfleet starve", notice)

    def test_a_burner_started_by_nzfleet_starve_is_left_alone(self):
        pid = self.run_agent()
        self.burner(ppid=pid, starved=True)
        self.stop_burners()
        self.assertEqual(self.killed, [])

    def test_a_burner_under_ten_minutes_is_left_alone(self):
        pid = self.run_agent()
        self.burner(ppid=pid, age=9 * MIN)
        self.stop_burners()
        self.assertEqual(self.killed, [])

    def test_a_burner_in_an_agents_cgroup_is_stopped_with_its_owner_named_and_the_pr_commented(self):
        pid = self.run_agent()
        self.table = [Proc(pid, 1, pid, "claude", 0, 0.0, "/", False, cgroup=SCOPE)]
        self.table.append(Proc(901, 1, 901, "yes", 13 * MIN, 600.0, "/elsewhere", False, ("yes",), SCOPE))
        with state.change() as s:
            s["agents"]["app-data"]["pr"] = {"number": 77, "how": "stated"}
        self.stop_burners()
        self.assertEqual(self.killed, [901])
        self.assertIn("app-data", self.told[-1])
        self.assertEqual([n for n, _ in self.comments], [77])

    def test_a_timeout_wrapping_yes_in_a_run_scope_is_stopped_but_one_wrapping_cargo_is_not(self):
        self.table = [Proc(902, 1, 902, "timeout", 13 * MIN, 0.0, "/", False, ("timeout", "7200", "yes"), SCOPE),
                      Proc(903, 1, 903, "timeout", 13 * MIN, 0.0, "/", False, ("timeout", "7200", "cargo", "test"), SCOPE)]
        self.stop_burners()
        self.assertEqual(self.killed, [902])

    def test_a_burner_in_a_run_scope_under_starve_or_in_another_cgroup_is_left_alone(self):
        self.table = [Proc(904, 1, 904, "yes", 13 * MIN, 0.0, "/", True, ("yes",), SCOPE),
                      Proc(905, 1, 905, "yes", 13 * MIN, 0.0, "/", False, ("yes",), "/system.slice/ssh.service")]
        self.stop_burners()
        self.assertEqual(self.killed, [])

    def test_the_newer_of_two_batches_over_the_same_prs_is_stopped_without_being_counted_a_failure(self):
        with state.change() as s:
            for bid, started, pid in (("core-1", 100, 700), ("core-2", 200, 701)):
                s["batches"][bid] = {"status": "running", "members": [[5, "x"]], "started": started, "pid": pid, "group": "core"}
        self.table = [Proc(700, 1, 700, "python3", 0, 0.0, "/", False), Proc(701, 1, 701, "python3", 0, 0.0, "/", False)]
        self.hourly()
        self.assertEqual(self.groups, [701])
        batches = state.read()["batches"]
        self.assertEqual((batches["core-1"]["status"], batches["core-2"]["status"]), ("running", "superseded"))
        self.assertEqual(state.read()["untied"], {})

    def test_the_hourly_supervisor_leaves_disk_pruning_and_tidy_to_their_single_schedules(self):
        lines = self.hourly()
        self.assertFalse(any("tidied" in line or "prun" in line for line in lines))

    def test_a_green_set_that_was_never_emailed_is_emailed(self):
        with state.change() as s:
            s["notified"] = [4]
            s["passed"]["4"] = {"sha": "abc", "main": "m", "order": 0, "batch": "b"}
            s["trains"] = {"t": {**train.make("m", [(4, "abc")], "tree"), "id": "t"}}
        with mock.patch.object(owner_tick.world, "pr_text", return_value=("Hold a rate", "A quote keeps its rate.")):
            self.hourly()
            self.hourly()
        (subject, body), = self.mails
        self.assertIn("Hold a rate", body)

    def test_an_hour_with_nothing_wrong_writes_a_health_line_and_emails_nobody(self):
        self.run_agent()
        lines = self.hourly()
        (health,) = [l for l in lines if l.startswith("health:")]
        self.assertIn("cpu 40% of", health)
        self.assertIn("1 agents running", health)
        self.assertIn("app-data", health)
        self.assertEqual(self.mails, [])
        self.assertEqual(self.events, lines)

    def test_the_pass_is_due_once_an_hour(self):
        self.assertFalse(supervise_tick.due(NOW, NOW + 59 * MIN))
        self.assertTrue(supervise_tick.due(NOW, NOW + 60 * MIN))
        self.hourly()
        self.assertEqual(state.read()["supervised"], NOW)


class IdleChains(Supervised):
    def queue(self, **record):
        """A chain `app` of two tasks; the first ended having stated `PR: none`."""
        with state.change() as s:
            s["todo"] = [{"key": "app-data", "area": "app", "task": "t", "model": "sonnet"},
                         {"key": "app-ui", "area": "app", "task": "t", "model": "sonnet"}]
        self.start()
        pid = self.agents()["app-data"]["pid"]
        with state.change() as s:
            s["agents"]["app-data"].update(status="done", finished=NOW, pr=None, stated_no_pr=True, worktree=self.wt, **record)

    def test_a_pr_none_that_left_nothing_in_its_worktree_no_longer_holds_the_chain(self):
        self.queue()
        self.acts[self.wt] = QUIET
        self.hourly()
        self.assertTrue(state.read()["agents"]["app-data"]["no_change"])
        self.assertIsNone(runs.pr_problem("app-data", state.read()["agents"]))
        self.assertTrue(any("goes on" in t for t in self.told))

    def test_a_pr_none_with_work_in_its_worktree_stays_a_persons_call_and_is_reported(self):
        self.queue()
        self.acts[self.wt] = Activity(2, 5, NOW, NOW, None)
        lines = self.hourly()
        self.assertNotIn("no_change", state.read()["agents"]["app-data"])
        self.assertTrue(any("chain app is idle at app-ui" in l and "PR: none" in l for l in lines))

    def test_a_pr_none_whose_worktree_is_unknown_is_not_assumed_empty(self):
        self.queue()
        with state.change() as s:
            s["agents"]["app-data"]["worktree"] = ""
            s["worktrees"].clear()
        self.hourly()
        self.assertNotIn("no_change", state.read()["agents"]["app-data"])

    def test_a_chain_waiting_for_a_pr_to_pass_says_which_pr(self):
        self.queue()
        with state.change() as s:
            s["agents"]["app-data"].update(pr={"number": 7, "branch": "b", "how": "attached"}, stated_no_pr=False)
        lines = self.hourly({7: "waiting"})
        self.assertTrue(any("#7" in l and "app-ui" in l for l in lines))


class Rows(Supervised):
    def test_a_running_agent_shows_commits_files_phase_and_the_time_since_each_change(self):
        pid = self.run_agent()
        self.acts[self.wt] = Activity(1, 36, NOW - 5 * MIN, NOW - 2 * MIN, NOW - 20 * MIN)
        with state.change() as s:
            s["notes"]["app-data"] = [{"at": NOW - 8 * MIN, "phase": "test", "line": "running the store tests"}]
        self.watch()
        agent_rows, _ = supervise_tick.rows(state.read(), self.host, {})
        (row,) = agent_rows
        self.assertEqual((row.key, row.state, row.commits, row.files, row.phase, row.note),
                         ("app-data", "moving", 1, 36, "test", "running the store tests"))
        self.assertEqual((row.since_commit, row.since_change, row.since_test), (5 * MIN, 2 * MIN, 20 * MIN))

    def test_cpu_minutes_add_up_across_a_tasks_runs(self):
        self.run_agent()
        with state.change() as s:
            s["samples"]["app-data"] = {"cpu": 600.0, "at": NOW, "alive_at": NOW, "busy_at": NOW}
            s["samples"]["app-data~2"] = {"cpu": 1200.0, "at": NOW, "alive_at": NOW, "busy_at": NOW}
            s["agents"]["app-data~2"] = {**s["agents"]["app-data"], "status": "running"}
        (row,), _ = supervise_tick.rows(state.read(), self.host, {})
        self.assertEqual((row.key, row.cpu_minutes), ("app-data~2", 30.0))

    def test_a_task_that_finished_long_ago_is_not_listed(self):
        self.run_agent()
        with state.change() as s:
            s["agents"]["app-data"].update(status="done", finished=NOW - 2 * 24 * 3600)
        self.assertEqual(supervise_tick.rows(state.read(), self.host, {})[0], [])

    def test_a_stuck_task_is_shown_as_stuck(self):
        with state.change() as s:
            for n in range(1, 5):
                s["agents"][runs.run_key("app-data", n)] = {"status": "unfinished", "started": NOW - 100, "finished": NOW - 50, "pid": 1,
                                                          "task": "t", "model": "sonnet", "log": "", "worktree": self.wt}
        (row,), _ = supervise_tick.rows(state.read(), self.host, {})
        self.assertEqual(row.state, "stuck")

    def test_the_digest_carries_the_progress_table(self):
        self.run_agent()
        with state.change() as s:
            s["digest_sent"] = NOW - 25 * 3600
            s["digest"] = [["2026-10-08 10:00", "main moved"]]
        owner_tick.digest(NOW, lambda: supervise_tick.progress_text(self.host, {}))
        (subject, body), = self.mails
        self.assertIn("PROGRESS", body)
        self.assertIn("app-data", body)


if __name__ == "__main__":
    unittest.main()
