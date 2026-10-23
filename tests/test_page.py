import re
import unittest
from pathlib import Path

from nzfleet import page, redact

from snapshots import HOUR, NOW, running_agent, taken, with_state

TRAIN = {"id": "t1", "status": "open", "members": [[7, "abc"]]}
PLANTED = (
    "ghp_" + "a" * 36,
    "github_pat_" + "B" * 30,
    "sk-" + "c" * 30,
    "AKIA" + "D" * 16,
    (Path(__file__).parent / "testdata" / "planted-key.pem").read_text(),
    "token=hunter2hunter2",
    "Authorization: Bearer abcdefghijklmnop",
    "/home/owner/secrets/gcs-project.json",
    "~/.config/nzfleet/env",
    "/etc/nzela/status-token",
)
CREDENTIAL_SHAPES = re.compile(r"ghp_|github_pat_|sk-c|AKIA|BEGIN PRIVATE|MIIEv|hunter2|abcdefghijklmnop|gcs-project|\.config/nzfleet|status-token", re.I)


def leaky_shot():
    s = with_state(
        holds={"h": {"why": " ".join(PLANTED), "since": NOW - HOUR}},
        needs_person={"k": "decide " + PLANTED[0]},
        agents={"app-data": running_agent()},
        notes={"app-data": [{"at": NOW - 60, "phase": "test", "line": f"used {PLANTED[5]} from {PLANTED[7]}"}]},
    )
    return taken(state=s, secret_paths=("/etc/nzela/status-token",))


def merge_ready():
    s = with_state(trains={"t1": TRAIN}, notified=[7])
    return taken(state=s)


class ThePage(unittest.TestCase):
    def test_the_merge_command_is_one_tap_to_copy(self):
        shot = merge_ready()
        command = shot["needs_you"][0]["fix"]
        self.assertTrue(command)
        self.assertIn(f'<button type="button" data-copy="{command}">', page.html(shot))

    def test_it_refreshes_itself(self):
        self.assertIn('http-equiv="refresh"', page.html(taken()))

    def test_the_sections_come_in_the_status_order(self):
        text = page.html(taken())
        marks = ["Needs you", "Needs attention", "Pipeline", "Agents", "Machine", "Cost", "Held and stale"]
        self.assertEqual(sorted(marks, key=text.index), marks)

    def test_text_from_the_snapshot_is_escaped(self):
        s = with_state(holds={"h": {"why": "<script>alert(1)</script>", "since": NOW - HOUR}})
        self.assertNotIn("<script>alert(1)", page.html(taken(state=s)))

    def test_no_credential_survives_in_a_page_built_from_a_leaky_snapshot(self):
        self.assertIsNone(CREDENTIAL_SHAPES.search(page.html(leaky_shot(), ("/etc/nzela/status-token",))))

    def test_no_credential_survives_in_the_snapshot_itself(self):
        import json
        self.assertIsNone(CREDENTIAL_SHAPES.search(json.dumps(leaky_shot())))

    def test_the_issue_carries_the_headline_and_the_two_urgent_sections(self):
        text = page.issue_text(merge_ready(), "http://100.1.2.3:8787/")
        self.assertRegex(text, r"(?s)ready to merge.*\*\*Needs you\*\*.*\*\*Needs attention\*\*.*http://100\.1\.2\.3:8787/")

    def test_the_issue_is_not_edited_for_a_headline_that_only_aged(self):
        first, later = page.issue_text(taken(), "u"), page.issue_text(taken(machine={**taken()["machine"], "load": [9.0, 9.0, 9.0], "cores": 16}), "u")
        self.assertNotEqual(first, later)
        self.assertFalse(page.issue_due(later, {"core": first.split("\n\n", 1)[1], "at": NOW}, NOW + 60))

    def test_the_issue_is_edited_when_what_needs_a_person_changes(self):
        s = with_state(needs_person={"k": "decide"})
        body = page.issue_text(taken(state=s), "u")
        self.assertTrue(page.issue_due(body, {"core": "old", "at": NOW}, NOW + 60))


class Redaction(unittest.TestCase):
    def test_ordinary_text_is_left_alone(self):
        self.assertEqual(redact.text("#7 failed outside its code: docker pull timed out"), "#7 failed outside its code: docker pull timed out")


if __name__ == "__main__":
    unittest.main()
