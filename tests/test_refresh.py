import os
import tempfile
import unittest

from nzfleet import refresh

from test_shots import png

SENDERS = "frontend/admin/src/screens/Senders.tsx"
CONSOLE = "frontend/admin/screenshots/"
BLUE = png(40, 40, lambda x, y: (10, 10, 200, 255))
RED = png(40, 40, lambda x, y: (200, 10, 10, 255))
FAINT = png(40, 40, lambda x, y: (10, 10, 208, 255) if x == 3 else (10, 10, 200, 255))


class Drawn(unittest.TestCase):
    """A run's `shots/console` directory with the named files drawn before and after."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def draw(self, name, before, after, state="changed"):
        path = CONSOLE + name
        for side, data in (("before", before), ("after", after)):
            if data is not None:
                full = os.path.join(self.dir, "console", side, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                open(full, "wb").write(data)
        with open(os.path.join(self.dir, "console", "changed.txt"), "a") as f:
            f.write(f"{state}\t{path}\t\n")

    def kept(self, golden=()):
        return [path for _, path in refresh.moved(self.dir, set(golden))]

    def test_a_shot_that_moved_is_kept(self):
        self.draw("07-senders.png", BLUE, RED)
        self.assertEqual(self.kept(), [CONSOLE + "07-senders.png"])

    def test_a_shot_of_a_screen_no_member_touched_is_kept_too(self):
        self.draw("05-transfer-detail-waiting.png", BLUE, RED)
        self.assertEqual(self.kept(), [CONSOLE + "05-transfer-detail-waiting.png"])

    def test_a_moved_shot_no_touched_screen_draws_is_unowned(self):
        self.draw("07-senders.png", BLUE, RED)
        self.draw("05-transfer-detail-waiting.png", BLUE, RED)
        self.assertEqual(refresh.unowned(self.dir, [SENDERS], set()), [CONSOLE + "05-transfer-detail-waiting.png"])

    def test_antialiasing_noise_is_dropped(self):
        self.draw("07-senders.png", BLUE, FAINT)
        self.assertEqual(self.kept(), [])

    def test_a_golden_shot_that_moved_by_a_hair_is_kept(self):
        self.draw("07-senders.png", BLUE, FAINT)
        self.assertEqual(self.kept(golden=[CONSOLE + "07-senders.png"]), [CONSOLE + "07-senders.png"])

    def test_a_new_shot_and_a_gone_one_are_kept(self):
        self.draw("07-senders-new.png", None, RED, "new")
        self.draw("07-senders-old.png", BLUE, None, "gone")
        self.assertEqual(self.kept(), [CONSOLE + "07-senders-new.png", CONSOLE + "07-senders-old.png"])

    def test_the_run_reports_every_shot_that_moved_by_screen(self):
        self.draw("07-senders.png", BLUE, RED)
        self.draw("07-senders-dark.png", BLUE, RED)
        self.draw("05-transfer-detail.png", BLUE, FAINT)
        self.assertEqual(refresh.moved_summary(self.dir, set()), "2 shots moved: 07-senders (2)")


if __name__ == "__main__":
    unittest.main()
