import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from nzfleet import render, status_view

from snapshots import NOW, proc, taken


class TheStatusCommand(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "snapshot.json")

    def write(self, **over):
        with open(self.path, "w") as f:
            json.dump(taken(**over), f)

    def run_status(self, sections=(), as_json=False):
        out = StringIO()
        with redirect_stdout(out), mock.patch("subprocess.run", side_effect=AssertionError("shelled out")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("shelled out")):
            code = status_view.run(list(sections), as_json, self.path, NOW)
        return code, out.getvalue()

    def test_it_answers_from_the_file_without_running_anything(self):
        self.write()
        code, text = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("NEEDS YOU", text)

    def test_json_of_one_section_holds_only_that_section(self):
        self.write()
        _, text = self.run_status(["needs"], as_json=True)
        self.assertEqual(sorted(json.loads(text)), ["at", "attention", "needs_you", "version"])

    def test_a_missing_snapshot_says_how_to_make_one(self):
        code, text = self.run_status()
        self.assertEqual(code, 1)
        self.assertIn("nzfleet tick", text)

    def test_a_snapshot_older_than_five_minutes_is_refused(self):
        self.write()
        for as_json in (False, True):
            out = StringIO()
            with redirect_stdout(out):
                code = status_view.run([], as_json, self.path, NOW + status_view.MAX_AGE + 1)
            self.assertEqual(code, 1)
            self.assertIn("nzfleet tick", out.getvalue())
            self.assertNotIn("NEEDS YOU", out.getvalue())

    def test_a_snapshot_just_inside_five_minutes_is_shown(self):
        self.write()
        out = StringIO()
        with redirect_stdout(out):
            code = status_view.run([], False, self.path, NOW + status_view.MAX_AGE)
        self.assertEqual(code, 0)

    def test_a_planted_burner_shows_with_its_fix_in_the_text(self):
        self.write(procs=[proc(500, "yes", age=1200.0)])
        _, text = self.run_status(["needs"])
        self.assertIn("kill -TERM 500", text)

    def test_held_is_one_line_until_asked_for(self):
        self.write()
        self.assertEqual(render.held(taken()["held"], False)[0].count("\n"), 0)

    def test_progress_renders_as_the_agent_table(self):
        self.write()
        code, _ = self.run_status(["progress"])
        self.assertEqual(code, 0)

    def test_attention_shows_five_lines_and_counts_the_rest(self):
        shot = taken()
        shot["attention"] = [{"kind": "test", "subject": str(n), "text": f"issue {n}", "fix": "fix"} for n in range(8)]
        shot["headline"]["attention"] = 8
        with open(self.path, "w") as f:
            json.dump(shot, f)
        _, text = self.run_status(["attention"])
        self.assertEqual(text.count("issue "), 5)
        self.assertIn("3 more", text)
