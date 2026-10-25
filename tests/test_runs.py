import unittest

from nzfleet import plan, runs

WARNING = "Permission allow rule (/home/user/.claude/settings.json): Write(x) is not matched by file permission checks\n"


class Unfinished(unittest.TestCase):
    def test_an_empty_log_is_unfinished(self):
        self.assertTrue(runs.unfinished(""))

    def test_a_log_with_only_the_tools_warning_is_unfinished(self):
        self.assertTrue(runs.unfinished(WARNING))

    def test_a_log_that_stops_without_a_pr_line_is_unfinished(self):
        self.assertTrue(runs.unfinished(WARNING + "API Error: 529 overloaded\n"))

    def test_a_run_that_left_work_in_the_background_is_unfinished(self):
        self.assertTrue(runs.unfinished(WARNING + "The rerun is going in the background; I'll act on the result when it lands.\n"))

    def test_a_summary_naming_its_pr_is_finished(self):
        self.assertFalse(runs.unfinished(WARNING + "PR #830 opens the draft.\nI ran fmt, clippy and the touched tests; all passed.\n"))

    def test_a_pr_none_line_is_a_final_report(self):
        self.assertFalse(runs.unfinished("Nothing to change.\nPR: none\n"))

    def test_a_report_naming_its_pr_is_finished_whatever_it_says_about_a_stray_process(self):
        log = (WARNING + "Roles with root are in PR #859.\n- Cleanup: `nzfleet tick` was still running in the background when I finished.\n")
        self.assertFalse(runs.unfinished(log))

    def test_a_pr_number_in_an_early_line_does_not_count(self):
        log = "PR #1 was the old attempt.\n" + "\n".join(f"line {i}" for i in range(30)) + "\n"
        self.assertTrue(runs.unfinished(log))


class Lineage(unittest.TestCase):
    def test_a_run_key_names_its_task_and_its_attempt(self):
        self.assertEqual((runs.base_key("a-b~3"), runs.attempt("a-b~3")), ("a-b", 3))
        self.assertEqual((runs.base_key("a-b"), runs.attempt("a-b")), ("a-b", 1))

    def test_the_latest_run_is_the_last_unbroken_attempt(self):
        agents = {"a": {"n": 1}, "a~2": {"n": 2}, "a~4": {"n": 4}}
        self.assertEqual(runs.latest("a", agents)[0], "a~2")

    def test_a_task_with_no_run_is_queued(self):
        self.assertEqual(runs.run_state("a", {}), "queued")

    def test_an_unfinished_run_with_resumptions_left_is_resuming_not_stuck(self):
        self.assertEqual(runs.run_state("a", {"a": {"status": "unfinished"}}), "resuming")

    def test_an_unfinished_run_with_none_left_is_stuck(self):
        agents = {k: {"status": "unfinished"} for k in ("a", "a~2")}
        self.assertEqual(runs.run_state("a", agents), "stuck")

    def test_a_task_waiting_on_a_resumed_one_starts_when_the_resumption_is_done(self):
        todo = [{"key": "b", "task": "", "after": ["a"]}]
        agents = {"a": {"status": "unfinished"}, "a~2": {"status": "done"}}
        self.assertEqual(plan.next_work(todo, agents, 0, 4, False)["key"], "b")


class Resuming(unittest.TestCase):
    def unfinished(self, finished=1000.0, **more):
        return {"status": "unfinished", "finished": finished, **more}

    def test_an_unfinished_run_is_resumed_once_its_wait_has_passed(self):
        agents = {"a": self.unfinished()}
        self.assertFalse(runs.needs_resume(agents["a"], agents, "a", 1000 + runs.BACKOFF[0] - 1))
        self.assertTrue(runs.needs_resume(agents["a"], agents, "a", 1000 + runs.BACKOFF[0]))

    def test_a_task_gets_one_retry_and_a_wait_for_it(self):
        self.assertEqual((runs.RESUME_LIMIT, len(runs.BACKOFF)), (1, 1))

    def test_a_run_that_has_a_successor_is_not_resumed_again(self):
        agents = {"a": self.unfinished(), "a~2": {"status": "running"}}
        self.assertFalse(runs.needs_resume(agents["a"], agents, "a", 10 ** 9))

    def test_one_retry_is_made_and_a_second_is_not(self):
        agents = {"a": self.unfinished()}
        self.assertTrue(runs.needs_resume(agents["a"], agents, "a", 10 ** 9))
        agents["a~2"] = self.unfinished()
        self.assertFalse(runs.needs_resume(agents["a~2"], agents, "a~2", 10 ** 9))

    def test_a_nudged_run_is_resumed_at_once(self):
        agents = {"a": self.unfinished(nudge=True)}
        self.assertTrue(runs.needs_resume(agents["a"], agents, "a", 1000))

    def test_a_finished_run_is_never_resumed(self):
        self.assertFalse(runs.needs_resume({"status": "done", "finished": 0}, {}, "a", 10 ** 9))

    def test_the_brief_says_where_the_run_stopped(self):
        where = runs.Stopped("/w/app", ("abc123 Add the store",), (" M a.kt", "?? b.kt"))
        task = runs.resume_task("do it", where, False)
        for part in ("/w/app", "abc123 Add the store", " M a.kt", "?? b.kt", "do it", "do not start over"):
            self.assertIn(part, task)

    def test_a_long_report_is_cut_and_says_how_much_was_left_out(self):
        where = runs.Stopped("/w", tuple(f"c{i}" for i in range(40)), tuple(f" M f{i}" for i in range(36)))
        task = runs.resume_task("do it", where, False)
        self.assertIn("and 30 more", task)
        self.assertIn("and 6 more", task)

    def test_a_nudge_asks_where_it_is_and_what_blocks_it(self):
        self.assertIn("what is blocking", runs.resume_task("do it", None, True))


class Stuck(unittest.TestCase):
    def exhausted(self, **more):
        return {"a": {"status": "unfinished", "finished": 1},
                "a~2": {"status": "unfinished", "finished": 2, "summary": "one\ntwo\n", "worktree": "/w/a", **more}}

    def test_a_task_that_used_its_retry_is_stuck_with_its_log_and_worktree(self):
        (stuck,) = runs.stuck_runs(self.exhausted(), 100)
        self.assertEqual((stuck.key, stuck.runs, stuck.since, stuck.tail, stuck.worktree), ("a", 2, 2, ("one", "two"), "/w/a"))

    def test_a_task_still_being_resumed_is_not_stuck(self):
        self.assertEqual(runs.stuck_runs({"a": {"status": "unfinished"}}, 100), [])

    def test_a_task_that_finished_after_resuming_is_not_stuck(self):
        agents = {**self.exhausted(), "a~2": {"status": "done"}}
        self.assertEqual(runs.stuck_runs(agents, 100), [])

    def test_the_sentence_names_the_worktree_the_last_lines_and_what_it_needs(self):
        (stuck,) = runs.stuck_runs(self.exhausted(), 100)
        text = runs.stuck_sentence(stuck, runs.Stopped("/w/a", ("c1",), (" M x",)))
        for part in ("/w/a", "two", "1 commits and 1 changed files", "nzfleet attach a <pr>", "will not be resumed again"):
            self.assertIn(part, text)

    def test_the_digest_line_says_how_long_it_has_been_stuck(self):
        (stuck,) = runs.stuck_runs(self.exhausted(), 100)
        (line,) = runs.stuck_digest_lines([stuck], 2 + 7 * 3600)
        self.assertIn("a has been stuck for 7 h", line)


class Attached(unittest.TestCase):
    def test_an_attached_pr_outranks_one_guessed_on_a_later_run(self):
        agents = {"a": {"status": "done", "pr": {"number": 5, "branch": "b", "how": "attached"}},
                  "a~2": {"status": "done", "pr": {"number": 9, "branch": "c", "how": "guessed"}}}
        self.assertIsNone(runs.pr_problem("a", agents))
        self.assertEqual(runs.recorded_pr("a", agents)["number"], 5)

    def test_a_guessed_pr_alone_needs_a_person(self):
        agents = {"a": {"status": "done", "pr": {"number": 9, "branch": "c", "how": "guessed"}}}
        self.assertTrue(runs.pr_problem("a", agents)[1])

    def test_pr_none_with_no_commits_in_the_worktree_blocks_nothing(self):
        agents = {"a": {"status": "done", "pr": None, "stated_no_pr": True, "no_change": True}}
        self.assertIsNone(runs.pr_problem("a", agents))

    def test_pr_none_with_work_in_the_worktree_is_a_persons_call(self):
        agents = {"a": {"status": "done", "pr": None, "stated_no_pr": True}}
        self.assertTrue(runs.pr_problem("a", agents)[1])


if __name__ == "__main__":
    unittest.main()
