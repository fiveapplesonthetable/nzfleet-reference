import os
import tempfile
import unittest
from unittest import mock

from nzfleet import config, cpu_budget, machine
from tests.isolated_config import isolate

STAT = "usage_usec {}\nuser_usec 0\nsystem_usec 0\n"


class Cgroups:
    """A fake cgroup v2 tree: each directory maps to its cpu.max, and the leaf also has cpu.stat."""

    def __init__(self, root: str):
        self.root = root

    def write(self, rel: str, **files: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(path, exist_ok=True)
        for name, text in files.items():
            with open(os.path.join(path, name.replace("_", ".")), "w") as handle:
                handle.write(text)


class TheFleetsBudget(unittest.TestCase):
    def setUp(self):
        isolate(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Cgroups(tmp.name)
        self.tree.write("user.slice", cpu_max="max 100000\n", cpu_stat=STAT.format(0))
        self.tree.write("user.slice/user-1000.slice", cpu_max="2400000 100000\n", cpu_stat=STAT.format(0))
        self.tree.write("user.slice/user-1000.slice/app.slice/nzfleet.service", cpu_max="max 100000\n", cpu_stat=STAT.format(0))
        self.own = "0::/user.slice/user-1000.slice/app.slice/nzfleet.service\n"
        self.slice = "user.slice/user-1000.slice"

    def set_usage(self, usec: int) -> None:
        self.tree.write(self.slice, cpu_stat=STAT.format(usec))

    def meter(self) -> cpu_budget.Meter:
        return cpu_budget.Meter(self.tree.root, self.own)

    def test_the_budget_is_the_nearest_ancestor_with_a_quota(self):
        budget = cpu_budget.find(self.tree.root, self.own)
        self.assertEqual((budget.path, budget.cpus), (os.path.join(self.tree.root, self.slice), 24.0))

    def test_a_host_without_a_quota_has_no_budget(self):
        self.tree.write(self.slice, cpu_max="max 100000\n")
        self.assertIsNone(cpu_budget.find(self.tree.root, self.own))

    def test_a_host_without_cgroup_v2_has_no_budget(self):
        self.assertIsNone(cpu_budget.find("/nonexistent/cgroup", self.own))
        self.assertIsNone(cpu_budget.find(self.tree.root, "12:cpu:/legacy\n"))

    def test_a_slice_using_its_quota_is_busy(self):
        meter = self.meter()
        meter.sample(now=0.0)
        self.set_usage(30 * 24 * 1_000_000)
        self.assertEqual(meter.sample(now=30.0), 100.0)

    def test_a_slice_using_a_third_of_its_quota_is_not_busy(self):
        meter = self.meter()
        meter.sample(now=0.0)
        self.set_usage(30 * 8 * 1_000_000)
        self.assertAlmostEqual(meter.sample(now=30.0), 100 / 3)

    def test_a_reading_inside_the_window_repeats_the_last_one(self):
        meter = self.meter()
        meter.sample(now=0.0)
        self.set_usage(30 * 24 * 1_000_000)
        meter.sample(now=30.0)
        self.set_usage(31 * 24 * 1_000_000 + 5_000_000_000)
        self.assertEqual(meter.sample(now=31.0), 100.0)

    def test_the_first_reading_has_nothing_to_compare_with(self):
        self.assertIsNone(self.meter().sample(now=0.0))

    def test_busy_compares_the_percentage_with_the_configured_limit(self):
        with mock.patch.object(config, "MAX_CPU_PERCENT", 80.0), \
                mock.patch.object(machine, "METER", mock.Mock(sample=mock.Mock(return_value=85.0))):
            self.assertTrue(machine.busy())
        with mock.patch.object(config, "MAX_CPU_PERCENT", 80.0), \
                mock.patch.object(machine, "METER", mock.Mock(sample=mock.Mock(return_value=60.0))):
            self.assertFalse(machine.busy())

    def test_busy_falls_back_to_the_load_average_where_there_is_no_reading(self):
        with mock.patch.object(config, "MAX_CPU_PERCENT", 50.0), \
                mock.patch.object(config.os, "sched_getaffinity", return_value=set(range(36))), \
                mock.patch.object(machine, "METER", mock.Mock(sample=mock.Mock(return_value=None))), \
                mock.patch.object(machine.os, "getloadavg", return_value=(19.0, 10.0, 0.0)):
            self.assertTrue(machine.busy())
        with mock.patch.object(config, "MAX_CPU_PERCENT", 50.0), \
                mock.patch.object(config.os, "sched_getaffinity", return_value=set(range(36))), \
                mock.patch.object(machine, "METER", mock.Mock(sample=mock.Mock(return_value=None))), \
                mock.patch.object(machine.os, "getloadavg", return_value=(17.0, 10.0, 0.0)):
            self.assertFalse(machine.busy())


class TheCpuLimitSetting(unittest.TestCase):
    def limit(self, percent=None, load=None):
        env = {k: v for k, v in (("NZFLEET_MAX_CPU_PERCENT", percent), ("NZFLEET_MAX_LOAD", load)) if v is not None}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(config.os, "sched_getaffinity", return_value=set(range(36))):
            for name in {"NZFLEET_MAX_CPU_PERCENT", "NZFLEET_MAX_LOAD"} - env.keys():
                os.environ.pop(name, None)
            return config.cpu_limit()

    def test_a_percentage_is_taken_as_it_is(self):
        self.assertEqual(self.limit(percent="75"), (75.0, None))

    def test_an_old_load_value_becomes_the_share_of_the_cpus_it_was(self):
        limit, problem = self.limit(load="18")
        self.assertEqual((limit, problem), (50.0, None))

    def test_an_old_load_value_above_the_cpus_is_a_gate_that_never_closes(self):
        self.assertEqual(self.limit(load="60"), (100.0, None))

    def test_the_percentage_wins_over_an_old_load_value(self):
        self.assertEqual(self.limit(percent="60", load="18"), (60.0, None))

    def test_nothing_set_uses_the_default(self):
        self.assertEqual(self.limit(), (config.DEFAULT_CPU_PERCENT, None))

    def test_an_out_of_range_percentage_is_refused_by_name(self):
        limit, problem = self.limit(percent="250")
        self.assertEqual(limit, config.DEFAULT_CPU_PERCENT)
        self.assertIn("NZFLEET_MAX_CPU_PERCENT", problem)

    def test_an_old_load_value_beyond_the_cpus_is_refused_by_name(self):
        limit, problem = self.limit(load="100000")
        self.assertEqual(limit, config.DEFAULT_CPU_PERCENT)
        self.assertIn("NZFLEET_MAX_LOAD", problem)


if __name__ == "__main__":
    unittest.main()
