import datetime
import tempfile
import unittest

from nzfleet import disktriage, jev, tidy


class FixedAnswer(jev.Decider):
    def __init__(self, choice, confidence):
        self.choice, self.confidence = choice, confidence

    def choose(self, _state, menu, _instructions):
        probabilities = {value: 1 / len(menu.items) for value, _label in menu.items}
        return jev.Verdict(self.choice, "jev", self.confidence, probabilities)


class DiskTriage(unittest.TestCase):
    def test_only_an_unlocked_allow_list_cache_can_be_selected(self):
        caches = (tidy.Sized("/cache/locked", 8, 8, True), tidy.Sized("/cache/free", 5, 8, False))
        selected, decision_id = disktriage.clear_choice(
            caches, jev.Rule(lambda _state, menu: next(value for value in menu if value.startswith("clear:"))),
            jev.Log(tempfile.mkdtemp()), datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc))
        self.assertEqual(selected, "/cache/free")
        self.assertTrue(decision_id.startswith("d-"))

    def test_a_locked_cache_is_never_offered_to_the_decider(self):
        caches = (tidy.Sized("/cache/locked", 8, 8, True),)
        seen = []

        def choose(_state, menu):
            seen.extend(menu)
            return "nothing"

        selected, _decision_id = disktriage.clear_choice(
            caches, jev.Rule(choose), jev.Log(tempfile.mkdtemp()),
            datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc))
        self.assertIsNone(selected)
        self.assertFalse(any(value.startswith("clear:") for value in seen))

    def test_a_clear_action_needs_the_point_floor_of_eighty_percent(self):
        caches = (tidy.Sized("/cache/free", 5, 8, False),)
        at_floor, _decision_id = disktriage.clear_choice(
            caches, FixedAnswer("clear:cache_0", 0.8), jev.Log(tempfile.mkdtemp()),
            datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc))
        below_floor, _decision_id = disktriage.clear_choice(
            caches, FixedAnswer("clear:cache_0", 0.79), jev.Log(tempfile.mkdtemp()),
            datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc))
        self.assertEqual(at_floor, "/cache/free")
        self.assertIsNone(below_floor)


if __name__ == "__main__":
    unittest.main()
