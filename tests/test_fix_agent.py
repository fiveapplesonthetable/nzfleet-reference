import time
import unittest
from unittest import mock

from nzfleet import agents, batch, config, fixes, plan, state, tick

from fakeworld import World, Runner
from test_owner_loop import FAILING, NO_SPACE, other


class Started:
    """Stands in for `agents.start`: records the call and leaves the run record the real one would."""

    def __init__(self):
        self.calls = []

    def __call__(self, key, task, model="sonnet", dry_run=False, area="", worktree="", pr=None, resume_session=""):
        self.calls.append((key, task, model))
        with state.change() as s:
            s["agents"][key] = {"status": "running", "pid": 1, "task": task, "model": model, "log": "", "started": time.time()}
        return f"started a {model} agent for {key}"


def finish(key, status="done"):
    with state.change() as s:
        s["agents"][key]["status"] = status


class FixAgent(unittest.TestCase):
    def setUp(self):
        self.world = World().__enter__()
        self.addCleanup(self.world.__exit__, None, None, None)
        self.started = Started()
        patch = mock.patch.object(agents, "start", self.started)
        patch.start()
        self.addCleanup(patch.stop)

    def red_pr(self, log=FAILING):
        w = self.world
        w.runner = Runner(logs={1: log})
        w.open_pr(1, {other(1): "one\n"})

    def test_a_pr_a_red_batch_blamed_gets_one_fix_agent(self):
        self.red_pr()
        self.world.tick()
        (key, task, model), = self.started.calls
        self.assertEqual(key, "fix-1")
        self.assertEqual(model, "sonnet")
        for part in ("#1", "pr1", "rust", "money::tests::adds"):
            self.assertIn(part, task)

    def test_the_fix_agent_is_not_started_again_while_it_runs_or_after_it_finishes(self):
        self.red_pr()
        self.world.tick()
        self.world.tick()
        finish("fix-1")
        self.world.tick()
        self.assertEqual(len(self.started.calls), 1)

    def test_a_head_that_is_red_again_after_the_fix_goes_to_the_owner_with_the_cause(self):
        self.red_pr()
        self.world.tick()
        finish("fix-1")
        self.world.open_pr(1, {other(1): "one fixed badly\n"})
        self.world.tick()
        self.world.tick()
        (subject, body), = self.world.mailed("#1")
        self.assertIn("still red", subject)
        self.assertIn("money::tests::adds", body)
        self.assertEqual(len(self.started.calls), 1)

    def test_a_fix_agent_that_ended_without_a_push_is_reported_once(self):
        self.red_pr()
        self.world.tick()
        self.world.tick()
        self.assertEqual(self.world.mailed("#1"), [])
        finish("fix-1")
        self.world.tick()
        self.world.tick()
        (subject, body), = self.world.mailed("#1")
        self.assertIn("pushed nothing", body)

    def test_a_failure_of_the_machine_gets_no_fix_agent(self):
        self.red_pr(NO_SPACE)
        self.world.tick()
        self.assertEqual(self.started.calls, [])

    def test_no_agent_starts_without_a_free_slot_and_it_starts_when_one_frees(self):
        self.red_pr()
        with mock.patch.object(config, "AGENT_SLOTS", 0):
            self.world.tick()
        self.assertEqual(self.started.calls, [])
        self.world.tick()
        self.assertEqual(len(self.started.calls), 1)


class TheBrief(unittest.TestCase):
    def test_it_names_the_branch_the_area_the_excerpt_and_the_steps(self):
        record = {"sha": "abc", "why": "failed: rust console", "areas": ["rust", "console"], "tests": ["a::b"], "log": "test a::b ... FAILED"}
        task = fixes.fix_task(7, "the-branch", record)
        for part in ("#7", "the-branch", "rust console", "test a::b ... FAILED", "root cause", "same branch", "nzfleet tick"):
            self.assertIn(part, task)

    def test_the_log_excerpt_keeps_the_budget_line(self):
        log = "compiling\nbundle is over its budget: 612 kB > 600 kB\nfailed: console\n"
        self.assertIn("over its budget", batch.excerpt(log))
