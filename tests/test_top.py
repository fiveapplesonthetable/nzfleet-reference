import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from nzfleet import top

from snapshots import NOW, running_agent, taken, with_state

EVENTS = [f"2026-10-10 17:0{i}:00 event number {i}" for i in range(8)]


def busy():
    agents = {f"agent-{i}": running_agent(f"agent-{i}", pid=900 + i) for i in range(6)}
    return taken(state=with_state(agents=agents))


class TheTopFrame(unittest.TestCase):
    def test_a_full_screen_shows_every_section_in_order(self):
        text = "\n".join(top.frame(busy(), EVENTS, 80, 120))
        titles = ["NEEDS YOU", "NEEDS ATTENTION", "PIPELINE", "AGENTS", "MACHINE", "COST", "HELD AND STALE", "EVENTS"]
        self.assertEqual(sorted(titles, key=text.index), titles)

    def test_an_80_by_24_terminal_is_filled_without_overflowing(self):
        lines = top.frame(busy(), EVENTS, 23, 80)
        self.assertLessEqual(len(lines), 23)
        self.assertTrue(all(len(line) <= 80 for line in lines))

    def test_a_short_screen_collapses_the_lower_sections_but_names_each(self):
        text = "\n".join(top.frame(busy(), EVENTS, 23, 80))
        for title in ("NEEDS YOU", "AGENTS", "MACHINE", "COST"):
            self.assertIn(title, text)
        self.assertIn("lines)", text)

    def test_the_newest_event_is_the_last_line(self):
        self.assertTrue(top.frame(busy(), EVENTS, 23, 80)[-1].endswith("event number 7"))

    def test_the_headline_comes_first_and_urgent_sections_are_never_collapsed(self):
        shot = taken(state=with_state(holds={"a": {"why": "w", "since": NOW - 100.0}}))
        lines = top.frame(shot, EVENTS, 10, 80)
        self.assertTrue(lines[0].startswith("main"))
        self.assertIn("NEEDS YOU", "\n".join(lines))

    def test_no_events_leaves_no_events_heading(self):
        self.assertNotIn("EVENTS", "\n".join(top.frame(busy(), [], 23, 80)))


class TheTopCommand(unittest.TestCase):
    def test_without_a_terminal_it_prints_one_frame_and_returns(self):
        d = tempfile.mkdtemp()
        snap, log = os.path.join(d, "snapshot.json"), os.path.join(d, "events.log")
        with open(snap, "w") as f:
            json.dump(busy(), f)
        with open(log, "w") as f:
            f.write("\n".join(EVENTS) + "\n")
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(top.run(snap, log), 0)
        self.assertIn("event number 7", out.getvalue())

    def test_a_missing_snapshot_says_how_to_make_one(self):
        out = StringIO()
        with redirect_stdout(out):
            top.run("/nonexistent/snapshot.json", "/nonexistent/events.log")
        self.assertIn("nzfleet tick", out.getvalue())
