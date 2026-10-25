import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from nzfleet import agents, cli, plan, snapshot, state, status_view, work

from fleet import Fleet
from snapshots import observed


class Notes(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        for patch in (mock.patch.object(state, "PATH", os.path.join(home, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(home, "state.lock"))):
            patch.start()
            self.addCleanup(patch.stop)

    def note(self, key, text):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.note(Namespace(key=key, text=text))
        return code, out.getvalue()

    def test_a_note_is_stored_under_the_task_even_when_given_a_resumed_runs_key(self):
        self.assertEqual(self.note("app-data~2", "test: running the store tests")[0], 0)
        (kept,) = state.read()["notes"]["app-data"]
        self.assertEqual((kept["phase"], kept["line"]), ("test", "running the store tests"))

    def test_a_note_with_an_unknown_phase_is_refused_with_the_phases_named(self):
        code, out = self.note("app-data", "dance: x")
        self.assertEqual(code, 1)
        self.assertIn("read, write, test, fix, push", out)
        self.assertEqual(state.read()["notes"], {})

    def test_only_the_newest_fifty_notes_are_kept(self):
        for n in range(60):
            self.note("app-data", f"write: step {n}")
        kept = state.read()["notes"]["app-data"]
        self.assertEqual((len(kept), kept[-1]["line"]), (50, "step 59"))

    def test_every_brief_ends_by_telling_the_agent_to_note_each_phase_and_to_starve_with_nzfleet(self):
        text = agents.brief("do it", "app-data~2")
        self.assertTrue(text.rstrip().endswith(plan.NOTE_INSTRUCTION.format(key="app-data").rstrip()))
        for part in ('nzfleet note app-data "<phase>: <one line>"', "nzfleet starve --"):
            self.assertIn(part, text)
        self.assertIn("one starved run of the touched test class", text)
        self.assertIn("never the whole suite", text)

    def test_a_fix_brief_says_never_to_loosen_a_check_and_to_stop_when_the_cause_is_not_found(self):
        for key in ("fix-7", "main-1a2b3c4d", "hands-9~1"):
            text = agents.brief("repair it", key)
            for part in ("Never lengthen a timeout", "raise a size budget", "stop without pushing", "nzfleet-guard limits"):
                self.assertIn(part, text, key)

    def test_a_brief_for_other_work_does_not_carry_the_fix_rules(self):
        self.assertNotIn("Never lengthen a timeout", agents.brief("do it", "app-data"))


class Claims(Fleet):
    def test_the_task_that_started_a_worktree_is_recorded_on_it(self):
        work.claim("app-data", "app-data")
        self.assertEqual(state.read()["worktrees"]["app-data"]["agent"], "app-data")

    def test_a_worktree_started_by_hand_records_no_task(self):
        with state.change() as s:
            s["worktrees"]["app-data"].pop("agent")
        work.claim("app-data", "")
        self.assertNotIn("agent", state.read()["worktrees"]["app-data"])


class Progress(Fleet):
    def test_the_progress_command_prints_the_table_from_the_snapshot(self):
        self.start()
        path = os.path.join(self.home, "snapshot.json")
        with open(path, "w") as f:
            json.dump(snapshot.snapshot(observed(state=state.read(), now=state.read()["agents"]["app-data"]["started"] + 60)), f)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(status_view.run(["progress"], False, path), 0)
        self.assertIn("app-data", out.getvalue())
        self.assertIn("CHAIN", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TheEventsCommand(unittest.TestCase):
    def run_events(self, lines, **args):
        path = os.path.join(tempfile.mkdtemp(), "events.log")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        out = io.StringIO()
        with mock.patch("nzfleet.tick.EVENTS", path), mock.patch("os.execvp", side_effect=AssertionError("followed")), redirect_stdout(out):
            code = cli.events(Namespace(since=args.get("since"), last=args.get("last", 20), follow=False))
        return code, out.getvalue()

    def test_it_prints_and_exits_without_following(self):
        code, text = self.run_events(["2026-10-10 17:00:00 a", "2026-10-10 17:01:00 b"], last=1)
        self.assertEqual((code, text), (0, "2026-10-10 17:01:00 b\n"))

    def test_an_unreadable_since_is_an_error(self):
        code, _ = self.run_events([], since="soon")
        self.assertEqual(code, 2)
