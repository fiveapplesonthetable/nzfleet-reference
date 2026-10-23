import unittest
from unittest import mock

from nzfleet import agents, config, owner, owner_tick, runs, state, tick

from fleet import Fleet, NOW


class Resuming(Fleet):
    def test_a_run_that_died_is_resumed_in_its_own_worktree_with_where_it_stopped(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        resumed = self.agents()["app-data~2"]
        self.assertEqual(resumed["worktree"], self.wt)
        for part in (self.wt, "The store reads from disk", "?? c.txt", "build the store"):
            self.assertIn(part, resumed["task"])

    def test_the_resume_prompt_reaches_the_agent_command(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertIn(self.wt, self.launched[-1][0][-1])

    def test_a_resume_waits_out_its_backoff(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0] - 1)
        self.assertNotIn("app-data~2", self.agents())

    def test_the_resumed_run_knows_its_task_by_the_original_key(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertEqual(self.launched[-1][1]["env"]["NZFLEET_AGENT_KEY"], "app-data")

    def test_a_held_task_is_not_resumed_until_it_is_released(self):
        self.start()
        self.die("app-data", NOW)
        with state.change() as s:
            s["holds"] = {"app-data": {"why": "waits for the console", "since": NOW}}
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertNotIn("app-data~2", self.agents())
        with state.change() as s:
            s["holds"] = {}
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertIn("app-data~2", self.agents())

    def test_automatic_retries_wait_when_agent_slots_are_zero(self):
        self.start()
        self.die("app-data", NOW)
        with mock.patch.object(config, "AGENT_SLOTS", 0):
            agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertNotIn("app-data~2", self.agents())
        self.assertEqual(len(self.launched), 1)

    def test_a_task_is_retried_once_and_then_left_stuck(self):
        self.start()
        self.die("app-data", NOW)
        at = NOW + runs.BACKOFF[0]
        agents.resume(now=at)
        self.assertIn("app-data~2", self.agents())
        self.die("app-data~2", at)
        agents.resume(now=at + 10 ** 6)
        self.assertNotIn("app-data~3", self.agents())
        self.assertEqual(runs.run_state("app-data", self.agents()), "stuck")

    def test_a_run_that_reported_is_not_resumed(self):
        self.start()
        self.die("app-data", NOW, "PR: #12\n")
        agents.resume(now=NOW + 10 ** 6)
        self.assertNotIn("app-data~2", self.agents())

    def test_the_worktree_is_found_by_when_it_was_made_for_a_run_that_started_before_the_record_existed(self):
        self.start()
        with state.change() as s:
            s["worktrees"]["app-data"].pop("agent")
        with mock.patch.object(agents.world, "worktrees_made_between", return_value=[self.wt]):
            self.die("app-data", NOW)
        self.assertEqual(self.agents()["app-data"]["worktree"], self.wt)


class Stuck(Fleet):
    def exhaust(self):
        self.start()
        self.die("app-data", NOW, "partial output\n")
        at = NOW + runs.BACKOFF[0]
        agents.resume(now=at)
        self.die("app-data~2", at, "I was reading the store when\nthe connection dropped\n")
        return at

    def test_a_stuck_task_emails_the_owner_once_naming_the_worktree_and_the_last_lines(self):
        at = self.exhaust()
        with mock.patch.object(tick.time, "time", return_value=at + 60):
            tick.owner_loop(False)
            tick.owner_loop(False)
        (subject, body), = self.mails
        self.assertIn("app-data", subject)
        for part in (self.wt, "the connection dropped", "1 commits and 1 changed files", "nzfleet attach app-data <pr>"):
            self.assertIn(part, body)

    def test_a_closed_pr_retires_its_stuck_task_without_owner_attention(self):
        at = self.exhaust()
        with state.change() as s:
            s["agents"]["app-data~2"]["pr"] = {"number": 1433, "how": "stated"}
        events = []
        with mock.patch.object(tick.world, "main_sha", return_value="main"), \
             mock.patch.object(tick.world, "fetch"), \
             mock.patch.object(tick.world, "merged_prs", return_value=set()), \
             mock.patch.object(tick.world, "head_of", return_value="head"), \
             mock.patch.object(tick.world, "is_ancestor", return_value=False), \
             mock.patch.object(owner_tick.world, "open_prs", return_value=[]), \
             mock.patch.object(tick.world, "pr_comments", return_value=(True, [])), \
             mock.patch.object(tick, "tell_event", side_effect=events.append), \
             mock.patch.object(tick.time, "time", return_value=at + 60):
            tick.owner_loop(False)
        self.assertEqual(self.mails, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(self.agents()["app-data~2"]["status"], "retired")

    def test_an_open_pr_keeps_its_stuck_task_in_owner_attention(self):
        at = self.exhaust()
        with state.change() as s:
            s["agents"]["app-data~2"]["pr"] = {"number": 1433, "how": "stated"}
        with mock.patch.object(tick.world, "main_sha", return_value="main"), \
             mock.patch.object(tick.world, "fetch"), \
             mock.patch.object(tick.world, "merged_prs", return_value=set()), \
             mock.patch.object(tick.world, "head_of", return_value="head"), \
             mock.patch.object(tick.world, "is_ancestor", return_value=False), \
             mock.patch.object(owner_tick.world, "open_prs", return_value=[{"number": 1433}]), \
             mock.patch.object(tick.world, "pr_comments", return_value=(False, [])), \
             mock.patch.object(tick.time, "time", return_value=at + 60):
            tick.owner_loop(False)
        self.assertEqual(self.agents()["app-data~2"]["status"], "unfinished")
        self.assertEqual(len(self.mails), 1)
        self.assertIn("app-data", self.mails[0][1])

    def test_a_task_still_being_resumed_sends_no_mail(self):
        self.start()
        self.die("app-data", NOW)
        with mock.patch.object(tick.time, "time", return_value=NOW + 60):
            tick.owner_loop(False)
        self.assertEqual(self.mails, [])

    def test_the_digest_lists_the_stuck_task_and_how_long_it_has_been_stuck_even_with_no_other_news(self):
        at = self.exhaust()
        with state.change() as s:
            s["digest_sent"] = at - 25 * 3600
        owner_tick.digest(at + 7 * 3600)
        (subject, body), = self.mails
        self.assertIn("app-data has been stuck for 7 h", body)

    def test_the_digest_is_not_sent_when_nothing_happened_and_nothing_is_stuck(self):
        with state.change() as s:
            s["digest_sent"] = NOW - 25 * 3600
        owner_tick.digest(NOW)
        self.assertEqual(self.mails, [])

    def test_status_calls_the_chain_stuck_only_after_the_resumptions(self):
        self.start()
        self.die("app-data", NOW)
        self.assertEqual(runs.run_state("app-data", self.agents()), "resuming")


if __name__ == "__main__":
    unittest.main()
