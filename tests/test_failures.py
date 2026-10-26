import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nzfleet import agents, cli, config, janitor, machine, plan, state, tick, world


class TickMaintenance(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        for name, value in (("PATH", "state.json"), ("LOCK", "state.lock")):
            patch = mock.patch.object(state, name, os.path.join(home.name, value))
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_prune_that_hangs_does_not_block_the_tick(self):
        with mock.patch.object(tick.machine, "prune_if_low", side_effect=TimeoutError("hung"), create=True) as prune, \
                mock.patch.object(tick, "tidy_machine"), mock.patch.object(agents, "reap", return_value=[]), \
                mock.patch.object(agents, "record_prs", return_value=[]), mock.patch.object(agents, "resume", return_value=[]), \
                mock.patch.object(tick, "barred_rebases", return_value=[]):
            tick.tidy(False)
        prune.assert_not_called()


class FailedPullRequests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        state.PATH = os.path.join(self.home, "state.json")
        state.LOCK = os.path.join(self.home, "state.lock")
        with state.change() as data:
            data["failed"] = {
                "788": {"sha": "0441c8b84a5382035e3c874b31c71b44dbc57a81",
                        "why": "failed: rust", "tests": []},
                "2": {"sha": "closed-head", "why": "failed: rust", "tests": []},
                "3": {"sha": "old-head", "why": "failed: rust", "tests": []},
                "4": {"sha": "current-head", "why": "failed: rust", "tests": []},
            }
            data["hands"] = {
                "837": {"sha": "709877d7109c6ad2ef0b928f0e88984f0f9b621a", "files": ["one.rs"]},
                "5": {"sha": "old-head", "files": ["two.rs"]},
            }

    def test_only_an_open_pr_at_the_failed_head_remains_actionable(self):
        open_prs = [
            {"number": 3, "headRefOid": "new-head"},
            {"number": 4, "headRefOid": "current-head"},
        ]

        self.assertEqual(tick.current_failures(state.read()["failed"], open_prs), {
            "4": {"sha": "current-head", "why": "failed: rust", "tests": []},
        })

    def test_next_skips_a_merged_788_style_failure_and_selects_the_current_open_head(self):
        open_prs = [
            {"number": 3, "headRefOid": "new-head"},
            {"number": 4, "headRefOid": "current-head"},
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(world, "open_prs", return_value=open_prs):
            cli.next_step(None)

        self.assertIn("Fix PR #4", output.getvalue())
        self.assertNotIn("#788", output.getvalue())
        self.assertNotIn("Rebase PR #837", output.getvalue())

    def test_tick_prunes_merged_closed_and_updated_head_records(self):
        open_prs = [
            {"number": 3, "headRefOid": "new-head"},
            {"number": 4, "headRefOid": "current-head"},
        ]

        tick.prune_stale(open_prs)

        data = state.read()
        self.assertEqual(set(data["failed"]), {"4"})
        self.assertEqual(set(data["hands"]), set())

    def test_a_dry_run_tick_does_not_write_state(self):
        with state.change() as data:
            data["batches"]["old"] = {"group": "core", "members": [], "areas": [],
                                       "main": "main", "started": 0, "status": "running", "pid": 10}
            data["conflicting"] = [837]
            data["todo"] = [{"key": "dry-run-agent", "task": "work", "model": "haiku"}]
        original = Path(state.PATH).read_bytes()
        with mock.patch.object(tick, "read", return_value=[]), \
                mock.patch.object(world, "open_prs", return_value=[]), \
                mock.patch.object(plan, "parents", return_value={}), \
                mock.patch.object(world, "main_sha", return_value="main"), \
                mock.patch.object(world, "ci_jobs", return_value=("completed", {}, 1)), \
                mock.patch.object(world, "owner_comments", return_value=[]), \
                mock.patch.object(world, "merges_cleanly", return_value=[]), \
                mock.patch.object(tick, "deploy"), \
                mock.patch.object(tick, "tell_event"), \
                mock.patch.object(agents, "_alive", return_value=False), \
                mock.patch.object(machine, "busy", return_value=False), \
                mock.patch.object(janitor, "run", return_value=[]), \
                mock.patch.object(state, "change", wraps=state.change) as change:
            tick.run(dry_run=True)

        change.assert_not_called()
        self.assertEqual(Path(state.PATH).read_bytes(), original)

    def test_next_does_not_select_a_stale_failure_when_github_cannot_be_read(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(
                world, "open_prs", side_effect=world.Failed("offline")):
            cli.next_step(None)

        self.assertNotIn("Fix PR #", output.getvalue())


if __name__ == "__main__":
    unittest.main()


class ConflictsThroughTheSlotLimit(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        state.PATH = os.path.join(self.home, "state.json")
        state.LOCK = os.path.join(self.home, "state.lock")

    def test_a_replay_that_needs_hands_starts_no_agent_itself(self):
        with mock.patch.object(agents, "start") as start, mock.patch.object(tick, "tell_event"):
            tick.settle_replay(5, "head", None, "hands", ["a.rs"])
        start.assert_not_called()
        self.assertEqual(state.read()["hands"]["5"], {"sha": "head", "files": ["a.rs"], "parent": None})

    def test_work_starts_a_rebase_before_queued_work_and_only_with_room(self):
        with state.change() as data:
            data["hands"] = {"5": {"sha": "h", "files": ["a.rs"], "parent": None}}
            data["todo"] = [{"key": "queued", "task": "x"}]
        with mock.patch.object(agents, "start", return_value="started") as start, \
                mock.patch.object(tick, "tell_event"), mock.patch.object(machine, "busy", return_value=False), \
                mock.patch.object(tick.world, "open_prs", return_value=[]), mock.patch.object(config, "AGENT_SLOTS", 1):
            tick.work(False)
        self.assertEqual(start.call_args.args[0], "hands-5")

    def test_work_starts_nothing_when_the_slots_are_full(self):
        with state.change() as data:
            data["hands"] = {"5": {"sha": "h", "files": ["a.rs"], "parent": None}}
            data["agents"] = {"other": {"status": "running"}}
        with mock.patch.object(agents, "start") as start, mock.patch.object(tick, "tell_event"), \
                mock.patch.object(machine, "busy", return_value=False), mock.patch.object(tick.world, "open_prs", return_value=[]), \
                mock.patch.object(config, "AGENT_SLOTS", 1):
            tick.work(False)
        start.assert_not_called()
