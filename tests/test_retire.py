import io
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from nzfleet import cli, holds, state

DAY = 24 * 3600
TODO = [{"key": "a", "task": "do a", "model": "sonnet"}, {"key": "b", "task": "do b", "model": "sonnet", "after": ["a", "pr:5"]}]
HELD = {"a": {"why": "waits for the lab", "since": 100.0}}


class Deciding(unittest.TestCase):
    def test_a_retired_task_leaves_the_queue_and_its_hold_and_is_recorded(self):
        todo, held, retired = holds.retire(TODO, HELD, {}, "a", " superseded by #12 ", 500.0)
        self.assertEqual(([t["key"] for t in todo], held), (["b"], {}))
        self.assertEqual(retired["a"], {"why": "superseded by #12", "at": 500.0, "task": "do a", "held_for": "waits for the lab"})

    def test_a_task_waiting_on_a_retired_one_no_longer_waits_for_it(self):
        todo, _, _ = holds.retire(TODO, HELD, {}, "a", "done", 500.0)
        self.assertEqual(todo[0]["after"], ["pr:5"])

    def test_retiring_needs_a_reason_a_queued_task_and_no_running_agent(self):
        self.assertIn("--why", holds.retire_problem("a", " ", TODO, {}))
        self.assertIn("not a queued task", holds.retire_problem("zzz", "x", TODO, {}))
        self.assertIn("running", holds.retire_problem("a", "x", TODO, {"a": {"status": "running"}}))
        self.assertEqual(holds.retire_problem("a", "x", TODO, {}), "")

    def test_a_hold_older_than_a_day_wants_a_decision_and_a_younger_one_does_not(self):
        held = {"a": {"why": "w", "since": 0.0}, "b": {"why": "w", "since": DAY}}
        self.assertEqual(holds.stale_holds(held, TODO, DAY + 1), ["a"])
        self.assertEqual(holds.stale_holds(held, TODO, 2 * DAY + 1), ["a", "b"])

    def test_a_batch_group_hold_is_never_stale_because_it_cannot_be_retired(self):
        self.assertEqual(holds.stale_holds({"core": {"why": "w", "since": 0.0}}, TODO, 9 * DAY), [])


class Commands(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        for patch in (mock.patch.object(state, "PATH", os.path.join(home, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")),
                      mock.patch.object(cli.world, "open_prs", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        with state.change() as s:
            s["todo"], s["holds"] = [dict(t) for t in TODO], dict(HELD)

    def say(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            code = fn(Namespace(**{"section": [], "json": False, **kw}))
        return code, out.getvalue()

    def test_a_retired_task_is_listed_by_todo_retired_and_gone_from_the_queue_and_status(self):
        self.assertEqual(self.say(cli.retire, key="a", why="superseded")[0], 0)
        self.assertIn("a ", self.say(cli.todo, key=None, retired=True)[1])
        self.assertIn("superseded", self.say(cli.todo, key=None, retired=True)[1])
        self.assertNotIn("do a", self.say(cli.todo, key=None, retired=False)[1])
        with mock.patch.object(cli.tick, "standings", return_value={}), mock.patch.object(cli.cloud, "login", side_effect=cli.world.Failed("x")):
            self.assertNotIn("waits for the lab", self.say(cli.status)[1])

    def test_retiring_a_task_that_is_not_queued_changes_nothing(self):
        code, said = self.say(cli.retire, key="zzz", why="x")
        self.assertEqual(code, 1)
        self.assertEqual(len(state.read()["todo"]), 2)
        self.assertIn("not a queued task", said)

    def test_next_says_to_release_or_retire_a_hold_older_than_a_day(self):
        with mock.patch.object(cli.time, "time", return_value=100.0 + DAY + 60):
            said = self.say(cli.next_step)[1]
        self.assertIn("decide: release or retire a", said)

    def test_next_is_quiet_about_a_fresh_hold(self):
        with mock.patch.object(cli.time, "time", return_value=200.0):
            self.assertNotIn("decide", self.say(cli.next_step)[1])


if __name__ == "__main__":
    unittest.main()
