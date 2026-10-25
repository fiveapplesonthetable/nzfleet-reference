import os
import unittest
from unittest import mock

from nzfleet import agents, cli, config, dispatch, machine, runs, state, tick, world

from fleet import Fleet, NOW

HEAD = "a" * 40
OPEN = dispatch.PullFacts("OPEN", HEAD, "nzf-branch", True, True)


class Dispatching(Fleet):
    def setUp(self):
        super().setUp()
        self.facts = OPEN
        for patch in (mock.patch.object(world, "pull_facts", side_effect=lambda n: self.facts),
                      mock.patch.object(agents, "COMMAND", "claude -p {model} {brief}"),
                      mock.patch.object(world, "comment", side_effect=lambda n, body: self.posted.append((n, body))),
                      mock.patch.object(machine, "busy", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.posted = []

    def dispatch(self, key="hands-1094", task="rebase #1094", pr=1094):
        return agents.start(key, task, pr=pr, worktree=self.wt)

    def refused(self):
        return state.read()["refused"]

    def test_a_managed_agent_gets_its_own_temporary_directory_for_every_tool_that_ignores_tmpdir(self):
        with mock.patch.object(config, "WORK", os.path.join(self.home, "work")):
            self.dispatch()
        env = self.launched[0][1]["env"]
        mine = os.path.join(self.home, "tmp", "hands-1094")
        self.assertEqual((env["TMPDIR"], env["JAVA_TOOL_OPTIONS"]), (mine, f"-Djava.io.tmpdir={mine}"))

    def test_a_merged_pr_is_never_dispatched(self):
        self.facts = dispatch.PullFacts("MERGED", HEAD, "nzf-branch", True, True)
        self.assertIn("#1094 is merged", self.dispatch())
        self.assertEqual((self.launched, list(self.refused())), ([], ["hands-1094"]))

    def test_a_closed_pr_is_refused(self):
        self.facts = dispatch.PullFacts("CLOSED", HEAD, "nzf-branch", True, True)
        self.assertIn("#1094 is closed", self.dispatch())

    def test_a_branch_that_is_gone_is_refused(self):
        self.facts = dispatch.PullFacts("OPEN", HEAD, "nzf-branch", False, True)
        self.assertIn("nzf-branch of #1094 no longer exists", self.dispatch())
        self.assertEqual(self.launched, [])

    def test_a_head_that_is_gone_is_refused(self):
        self.facts = dispatch.PullFacts("OPEN", HEAD, "nzf-branch", True, False)
        self.assertIn("head aaaaaaaa of #1094 no longer exists", self.dispatch())
        self.assertEqual(self.launched, [])

    def test_a_refusal_is_said_once_while_its_reason_stands(self):
        self.facts = dispatch.PullFacts("MERGED", HEAD, "nzf-branch", True, True)
        self.dispatch()
        self.assertEqual(self.dispatch(), "")

    def test_a_pr_that_cannot_be_checked_is_not_started(self):
        with mock.patch.object(world, "pull_facts", side_effect=world.Failed("gh: offline")):
            self.assertIn("could not check #1094", self.dispatch())
        self.assertEqual(self.launched, [])

    def test_a_held_task_cannot_be_started_by_a_direct_dispatch(self):
        with state.change() as s:
            s["holds"]["hands-1094"] = {"why": "one owner is already retrying it", "since": NOW}
        self.assertIn("hands-1094 is held", self.dispatch())
        self.assertEqual(self.launched, [])

    def test_zero_agent_slots_refuse_a_direct_dispatch(self):
        with mock.patch.object(config, "AGENT_SLOTS", 0):
            self.assertIn("NZFLEET_AGENT_SLOTS is 0", self.dispatch())
        self.assertEqual(self.launched, [])

    def test_the_same_key_at_the_same_head_never_runs_twice(self):
        self.dispatch()
        self.die("hands-1094", NOW, "PR: #1094\n")
        self.assertEqual(self.dispatch(task="rebase #1094, files changed"), "")
        self.assertEqual(len(self.launched), 1)

    def test_the_same_key_at_a_new_head_runs_again(self):
        self.dispatch()
        self.die("hands-1094", NOW, "PR: #1094\n")
        self.facts = dispatch.PullFacts("OPEN", "b" * 40, "nzf-branch", True, True)
        self.dispatch(task="rebase #1094, files changed")
        self.assertEqual(len(self.launched), 2)

    def test_a_hands_task_for_a_merged_pr_leaves_the_queue_moving(self):
        self.facts = dispatch.PullFacts("MERGED", HEAD, "nzf-branch", True, True)
        with state.change() as s:
            s["hands"]["1094"] = {"files": ["a.rs"]}
            s["todo"].append({"key": "app-data", "task": "build the store", "model": "sonnet", "area": "", "after": []})
        with mock.patch.object(world, "open_prs", return_value=[{"number": 1094, "isDraft": False, "mergeable": "CONFLICTING",
                                                                  "headRefName": "nzf-branch"}]):
            tick.work(False)
        self.assertEqual(list(self.agents()), ["app-data"])
        self.assertIn("hands-1094", self.refused())

    def test_a_retry_resumes_the_first_session_instead_of_starting_over(self):
        self.dispatch()
        session = self.agents()["hands-1094"]["session"]
        self.assertEqual(self.launched[0][0][2:5], ["claude", "--session-id", session])
        self.die("hands-1094", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        argv = self.launched[1][0][2:]
        self.assertEqual(argv[:3], ["claude", "--resume", session])
        self.assertNotIn("rebase #1094", argv[-1])
        self.assertIn(self.wt, argv[-1])

    def test_a_legacy_claude_run_retries_with_the_active_codex_session_and_worktree(self):
        with mock.patch.object(agents, "AGENT_CODEX", "codex exec --model {model} {brief}"):
            self.dispatch()
            self.die("hands-1094", NOW)
            with state.change() as s:
                s["agent_session"] = {"provider": "codex", "model": "gpt-6-luna"}
                old = s["agents"]["hands-1094"]
                old.pop("provider_selection")
                old.pop("provider")
            agents.resume(now=NOW + runs.BACKOFF[0])
        argv = self.launched[1][0][2:]
        self.assertEqual(argv[:4], ["codex", "exec", "--model", "gpt-6-luna"])
        self.assertNotIn("--resume", argv)
        self.assertIn("rebase #1094", argv[-1])
        self.assertIn(self.wt, argv[-1])
        record = self.agents()["hands-1094~2"]
        self.assertEqual((record["provider"], record["model"], record["session"]), ("codex", "gpt-6-luna", ""))

    def test_an_explicit_claude_run_retries_under_the_active_codex_session(self):
        agents.start("hands-1094", "rebase #1094", provider_override="claude", worktree=self.wt)
        self.die("hands-1094", NOW)
        with state.change() as s:
            s["agent_session"] = {"provider": "codex", "model": "gpt-6-luna"}
        with mock.patch.object(agents, "AGENT_CODEX", "codex exec --model {model} {brief}"):
            agents.resume(now=NOW + runs.BACKOFF[0])
        argv = self.launched[1][0][2:]
        self.assertEqual(argv[:4], ["codex", "exec", "--model", "gpt-6-luna"])
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertIn("rebase #1094", argv[-1])
        self.assertIn(self.wt, argv[-1])

    def test_an_active_session_cannot_be_overridden_by_a_dispatch_caller(self):
        with state.change() as s:
            s["agent_session"] = {"provider": "codex", "model": "gpt-6-luna"}
        with mock.patch.object(agents, "AGENT_CODEX", "codex exec --model {model} {brief}"):
            agents.start("hands-1094", "task", model="sonnet", provider_override="claude")
        self.assertEqual(self.launched[0][0][2:6], ["codex", "exec", "--model", "gpt-6-luna"])
        self.assertEqual(self.agents()["hands-1094"]["provider"], "codex")

    def test_an_explicit_claude_selection_uses_a_claude_command(self):
        with mock.patch.object(agents, "COMMAND", "codex exec --model {model} {brief}"), \
                mock.patch.object(agents, "AGENT_CLAUDE", "claude -p --model {model} {brief}"):
            agents.start("hands-1094", "task", provider_override="claude")
        self.assertEqual(self.launched[0][0][2], "claude")
        self.assertEqual(self.agents()["hands-1094"]["provider"], "claude")

    def test_a_retry_of_a_pr_that_merged_meanwhile_is_refused(self):
        self.dispatch()
        self.die("hands-1094", NOW)
        self.facts = dispatch.PullFacts("MERGED", HEAD, "nzf-branch", True, True)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertEqual(len(self.launched), 1)
        self.assertNotIn("hands-1094~2", self.agents())

    def test_a_third_attempt_posts_the_log_tail_to_the_owner_issue_instead_of_dispatching(self):
        self.dispatch()
        self.die("hands-1094", NOW, "first\n")
        at = NOW + runs.BACKOFF[0]
        agents.resume(now=at)
        self.die("hands-1094~2", at, "".join(f"line {i}\n" for i in range(30)))
        with mock.patch.object(tick.world, "main_sha", return_value="main"), \
             mock.patch.object(tick.world, "fetch"), \
             mock.patch.object(tick.world, "merged_prs", return_value=set()), \
             mock.patch.object(tick.world, "head_of", return_value=HEAD), \
             mock.patch.object(tick.world, "is_ancestor", return_value=False), \
             mock.patch.object(tick.world, "pr_comments", return_value=(False, [])), \
             mock.patch.object(tick.world, "open_prs", return_value=[{"number": 1094}]):
            for _ in range(2):
                agents.resume(now=at + 10 ** 6)
                with mock.patch.object(tick.time, "time", return_value=at + 60):
                    tick.owner_loop(False)
        self.assertEqual(len(self.launched), 2)
        (number, body), = self.posted
        self.assertEqual(number, config.OWNER_ISSUE)
        self.assertIn("line 29", body)
        self.assertIn("line 10", body)
        self.assertNotIn("line 9\n", body)

    def test_no_brief_is_dispatched_more_than_twice(self):
        at = NOW
        for _ in range(4):
            self.dispatch()
            agents.resume(now=at)
            for key in list(self.agents()):
                if self.agents()[key]["status"] == "running":
                    self.die(key, at)
            at += 10 ** 6
        agents.resume(now=at)
        self.assertEqual(len(self.launched), 2)

    def test_a_launched_claude_agent_is_given_the_hook_settings(self):
        self.dispatch()
        argv, _ = self.launched[0]
        settings = argv[argv.index("--settings") + 1]
        self.assertIn("no-background-bash.py", open(settings).read())

    def test_a_command_that_is_not_claude_gets_no_settings_flag(self):
        self.assertEqual(dispatch.with_settings(["codex", "exec"], "/s.json"), ["codex", "exec"])

    def test_the_named_codex_session_uses_its_exact_provider_and_model(self):
        with mock.patch.object(agents, "AGENT_CODEX", "codex exec --model {model} {brief}"):
            cli.main(["session", "codex-fleet-session", "--provider", "codex"])
            self.dispatch()
        argv, _ = self.launched[0]
        self.assertEqual(argv[2:6], ["codex", "exec", "--model", "gpt-6-luna"])
        self.assertIn("rebase #1094", argv[-1])
        record = self.agents()["hands-1094"]
        self.assertEqual((record["provider"], record["model"]), ("codex", "gpt-6-luna"))

    def test_the_session_command_requires_a_name_and_provider(self):
        self.assertEqual(cli.main(["session", "codex-fleet-session"]), 2)
        self.assertEqual(state.read()["agent_session"], {})

    def test_a_non_codex_session_requires_an_explicit_model(self):
        self.assertEqual(cli.main(["session", "claude-session", "--provider", "claude"]), 2)
        self.assertEqual(state.read()["agent_session"], {})

    def test_the_generic_command_remains_in_effect_without_a_session_override(self):
        with mock.patch.object(agents, "COMMAND", "other-agent --model {model} {brief}"):
            self.dispatch()
        self.assertEqual(self.launched[0][0][2:5], ["other-agent", "--model", "sonnet"])

    def test_a_retry_keeps_the_provider_and_model_from_its_first_run(self):
        with mock.patch.object(agents, "AGENT_CODEX", "codex exec --model {model} {brief}"):
            cli.main(["session", "codex-fleet-session", "--provider", "codex", "--model", "gpt-6-luna"])
            self.dispatch()
            self.die("hands-1094", NOW)
            cli.main(["session", "other", "--provider", "claude", "--model", "opus"])
            agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertEqual(self.launched[1][0][2], "claude")
        self.assertEqual(self.agents()["hands-1094~2"]["provider"], "claude")
        self.assertEqual(self.agents()["hands-1094~2"]["model"], "opus")


if __name__ == "__main__":
    unittest.main()
