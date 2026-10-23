import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nzfleet import config, machine


class TheCpuLimitRefresh(unittest.TestCase):
    def busy_at(self, one: float, five: float) -> bool:
        with mock.patch.object(machine.config, "MAX_CPU_PERCENT", 90.0), \
                mock.patch.object(machine, "cpu_percent", return_value=None), \
                mock.patch.object(machine.config, "usable_cpus", return_value=36), \
                mock.patch.object(machine.os, "getloadavg", return_value=(one, five, 0.0)):
            return machine.busy()

    def test_load_fallback_holds_work_when_either_average_exceeds_the_quota_share(self):
        self.assertTrue(self.busy_at(33, 20))
        self.assertTrue(self.busy_at(20, 33))
        self.assertFalse(self.busy_at(30, 25))

    def test_machine_status_reports_affinity_cores(self):
        with mock.patch.object(machine.os, "cpu_count", return_value=72), \
                mock.patch.object(machine.os, "sched_getaffinity", return_value=set(range(36))), \
                mock.patch.object(machine.os, "getloadavg", return_value=(0.0, 0.0, 0.0)):
            self.assertEqual(machine.reading(proc="/nonexistent", mounts=())["cores"], 36)

    def test_low_disk_on_agent_storage_holds_new_work(self):
        paths = {"/srv/fleet": 8, config.HOME: 80, config.WORK: 80, config.REPO: 80, config.TARGETS: 80}
        with mock.patch.object(machine, "free_gb", side_effect=lambda path: paths[path]), \
                mock.patch.object(machine, "cpu_percent", return_value=0.0):
            self.assertTrue(machine.busy())

    def test_machine_status_includes_every_configured_storage_mount(self):
        mounts = ("/", "/dev/shm", "/srv/fleet", "/home", "/work", "/repo", "/targets")
        with mock.patch.object(config, "HOME", "/home"), \
                mock.patch.object(config, "WORK", "/work"), \
                mock.patch.object(config, "REPO", "/repo"), \
                mock.patch.object(config, "TARGETS", "/targets"), \
                mock.patch.object(machine, "free_gb", return_value=50), \
                mock.patch.object(machine.os, "getloadavg", return_value=(0.0, 0.0, 0.0)):
            self.assertEqual(tuple(row["mount"] for row in machine.reading(proc="/nonexistent")["disk"]), mounts)

    def test_an_invalid_limit_does_not_prevent_config_import(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = {"NZFLEET_ENV": "/dev/null", "NZFLEET_HOME": home.name,
               "NZFLEET_MAX_LOAD": "100000", "NZFLEET_MAX_CPU_PERCENT": "100000",
               "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}
        result = subprocess.run([sys.executable, "-c", "import nzfleet.config"], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_invalid_refresh_keeps_the_last_good_limit_and_reports_the_problem(self):
        from nzfleet import config
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(config, "_ENVIRONMENT", frozenset(set(config._ENVIRONMENT) - {"NZFLEET_AGENT_SLOTS", "NZFLEET_MAX_CPU_PERCENT"})), \
                mock.patch.object(config, "MAX_CPU_PERCENT", 36.0), \
                mock.patch.object(config, "CONFIG_PROBLEM", None):
            os.environ.pop("NZFLEET_AGENT_SLOTS", None)
            os.environ.pop("NZFLEET_MAX_CPU_PERCENT", None)
            path = os.path.join(home, "env")
            with open(path, "w") as env_file:
                env_file.write("NZFLEET_MAX_CPU_PERCENT=80\n")
            config.refresh(path)
            with open(path, "w") as env_file:
                env_file.write("NZFLEET_MAX_CPU_PERCENT=100000\n")
            config.refresh(path)
            self.assertEqual(config.MAX_CPU_PERCENT, 80.0)
            self.assertIn("NZFLEET_MAX_CPU_PERCENT", config.CONFIG_PROBLEM)

    def test_a_load_limit_is_reloaded_from_the_environment_file(self):
        from nzfleet import config
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(config, "_ENVIRONMENT", frozenset(set(config._ENVIRONMENT) - {"NZFLEET_AGENT_SLOTS", "NZFLEET_MAX_CPU_PERCENT"})):
            os.environ.pop("NZFLEET_AGENT_SLOTS", None)
            os.environ.pop("NZFLEET_MAX_CPU_PERCENT", None)
            path = os.path.join(home, "env")
            with open(path, "w") as env_file:
                env_file.write("NZFLEET_AGENT_SLOTS=2\nNZFLEET_MAX_CPU_PERCENT=80\n")
            config.refresh(path)
            self.assertEqual((config.AGENT_SLOTS, config.MAX_CPU_PERCENT), (2, 80.0))
            with open(path, "w") as env_file:
                env_file.write("NZFLEET_AGENT_SLOTS=1\nNZFLEET_MAX_CPU_PERCENT=60\n")
            config.refresh(path)
            self.assertEqual((config.AGENT_SLOTS, config.MAX_CPU_PERCENT), (1, 60.0))


if __name__ == "__main__":
    unittest.main()
