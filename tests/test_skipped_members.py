import unittest

from nzfleet import skips, state, statuses, tick

from fakeworld import Runner, World

CONFLICT = {"why": "CONFLICT", "files": ["tools/nzfleet/cli.py"]}


def code(n):
    return f"crates/nzela/src/pr{n}.rs"


class SkippedMembers(unittest.TestCase):
    def batch(self, w, skipped):
        for n in (1, 2, 3):
            w.open_pr(n, {code(n): f"{n}\n"})
        w.runner = Runner(skips=skipped)
        w.tick()
        return state.read()

    def test_a_batch_that_left_a_member_out_is_not_green_and_names_it(self):
        with World() as w:
            s = self.batch(w, {3: CONFLICT})
            b = next(b for b in s["batches"].values())
            self.assertEqual(b["status"], "partial")
            self.assertIn("skipped #3 (CONFLICT in tools/nzfleet/cli.py)", b["line"])

    def test_a_skipped_member_is_never_ready(self):
        with World() as w:
            s = self.batch(w, {3: CONFLICT})
            self.assertEqual((set(s["passed"]), 3 in s["notified"]), ({"1", "2"}, False))

    def test_a_conflicting_member_is_queued_for_a_rebase(self):
        with World() as w:
            s = self.batch(w, {3: CONFLICT})
            self.assertEqual(s["hands"]["3"], {"sha": w.head(3), "files": ["tools/nzfleet/cli.py"], "parent": None})

    def test_a_member_skipped_for_another_reason_is_named_but_not_rebased(self):
        with World() as w:
            s = self.batch(w, {3: {"why": "its head moved after the batch was formed", "files": []}})
            b = next(b for b in s["batches"].values())
            self.assertIn("#3 (its head moved after the batch was formed)", b["line"])
            self.assertNotIn("3", s["hands"])


class TheStatusPosted(unittest.TestCase):
    def test_a_conflict_is_a_failure_that_says_what_conflicted(self):
        state_, text = statuses.verdict(3, "partial", [1], [3], {}, [], {"3": CONFLICT})
        self.assertEqual(state_, "failure")
        self.assertIn("CONFLICT in tools/nzfleet/cli.py", text)

    def test_a_green_run_with_nothing_skipped_stays_green(self):
        self.assertEqual(skips.outcome("green", []), "green")


if __name__ == "__main__":
    unittest.main()
