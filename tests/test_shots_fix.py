import os
import tempfile
import unittest

from nzfleet import shotsfix
from nzfleet.shotsfix import Kind

from test_shots import png

ANDROID = "frontend/android/docs/screenshots/"
HOME = ANDROID + "03-home.png"
SIGNIN = ANDROID + "01-signin.png"
HOME_SCREEN = "frontend/android/app/src/main/java/app/nzela/ui/home/HomeScreen.kt"
THEME = "frontend/android/app/src/main/java/app/nzela/ui/theme/Theme.kt"
OLD = png(40, 80, lambda x, y: (10, 10, 200, 255))
TALL = png(40, 70, lambda x, y: (10, 10, 200, 255))
RED = png(40, 80, lambda x, y: (200, 10, 10, 255))


class Report(unittest.TestCase):
    """A run's shot-report directory for android with the named goldens drawn before and after."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def draw(self, path, before, after):
        for side, data in (("before", before), ("after", after)):
            full = os.path.join(self.dir, "android", side, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "wb").write(data)
        with open(os.path.join(self.dir, "android", "changed.txt"), "a") as f:
            f.write(f"changed\t{path}\tgolden\n")

    def verdicts(self, touched, catalogue=10, golden=(HOME, SIGNIN)):
        goldens, total = shotsfix.read_goldens(self.dir, set(golden))["android"]
        return shotsfix.classify(goldens, total, catalogue, touched)


class Canvas(Report):
    def test_a_shot_that_changed_size_is_a_rendering_break_naming_both_sizes(self):
        self.draw(HOME, OLD, TALL)
        (one,) = self.verdicts([THEME])
        self.assertEqual(one.kind, Kind.CANVAS)
        self.assertIn("every shot is 40x70, expected 40x80", one.line)
        self.assertIn("Theme.kt", one.line)

    def test_most_of_the_catalogue_moving_at_once_is_a_canvas_even_at_the_same_size(self):
        self.draw(HOME, OLD, RED)
        self.draw(SIGNIN, OLD, RED)
        kinds = {v.kind for v in self.verdicts([HOME_SCREEN], catalogue=3)}
        self.assertEqual(kinds, {Kind.CANVAS})

    def test_a_canvas_break_exits_non_zero_and_commits_nothing(self):
        self.draw(HOME, OLD, TALL)
        results = self.verdicts([HOME_SCREEN])
        self.assertEqual(shotsfix.verdict(results)[1], 1)
        self.assertNotIn(Kind.INTENDED, {r.kind for r in results})


class Intended(Report):
    def test_a_golden_whose_screen_the_diff_touches_is_intended(self):
        self.draw(HOME, OLD, RED)
        (one,) = self.verdicts([HOME_SCREEN])
        self.assertEqual(one.kind, Kind.INTENDED)
        self.assertEqual(shotsfix.verdict([one])[1], 0)


class Drift(Report):
    def test_a_golden_nothing_in_the_diff_reaches_is_drift_with_its_pixel_delta(self):
        self.draw(SIGNIN, OLD, RED)
        (one,) = self.verdicts([HOME_SCREEN])
        self.assertEqual(one.kind, Kind.DRIFT)
        self.assertIn("50.000% of its channels differ", one.line)
        self.assertEqual(shotsfix.verdict([one])[1], 1)

    def test_a_noise_move_of_a_non_golden_shot_is_not_reported(self):
        self.draw(ANDROID + "09-other.png", OLD, RED)
        self.assertEqual(self.verdicts([HOME_SCREEN]), [])
        self.assertEqual(shotsfix.verdict([]), ("no golden screenshot moved", 0))
