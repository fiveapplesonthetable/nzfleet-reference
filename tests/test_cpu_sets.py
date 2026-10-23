import unittest

from nzfleet import areas


class ConcurrentAreasHaveDisjointCpuSets(unittest.TestCase):
    def test_each_area_receives_no_more_than_its_configured_cpu_count(self):
        sets = areas.cpu_sets({"rust": 7, "android": 6, "console": 6}, set(range(36, 72)))
        self.assertEqual(len(sets["console"]), 6)
        self.assertTrue(all(sets[a] for a in ("rust", "android")))
        self.assertEqual(sum(map(len, sets.values())), 19)
        self.assertEqual(len(set().union(*map(set, sets.values()))), 19)

    def test_each_area_gets_a_cpu_or_the_allocation_is_refused(self):
        self.assertEqual(areas.cpu_sets({"rust": 1, "android": 1}, {8, 9, 10, 11}), {"rust": (8,), "android": (9,)})
        with self.assertRaisesRegex(ValueError, "cannot give each area its own CPU"):
            areas.cpu_sets({"rust": 7, "android": 6, "console": 6}, set(range(4)))


if __name__ == "__main__":
    unittest.main()
