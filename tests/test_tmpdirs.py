import os
import stat
import tempfile
import unittest
from unittest import mock

from nzfleet import config, tmpdirs, units


class Environment(unittest.TestCase):
    def test_tmpdir_and_the_jvm_property_point_at_the_directory(self):
        env = tmpdirs.environment("/t/a", {"PATH": "/bin"})
        self.assertEqual((env["TMPDIR"], env["PATH"], env["JAVA_TOOL_OPTIONS"]), ("/t/a", "/bin", "-Djava.io.tmpdir=/t/a"))

    def test_other_jvm_options_are_kept_and_an_old_tmpdir_is_replaced(self):
        env = tmpdirs.environment("/t/a", {"JAVA_TOOL_OPTIONS": "-Xmx2g -Djava.io.tmpdir=/tmp"})
        self.assertEqual(env["JAVA_TOOL_OPTIONS"], "-Xmx2g -Djava.io.tmpdir=/t/a")

    def test_a_task_key_cannot_climb_out_of_the_root(self):
        self.assertEqual(tmpdirs.agent_directory("/t", "../x/y"), "/t/..-x-y")

    def test_only_our_prefixes_are_system_leftovers(self):
        self.assertEqual([tmpdirs.system_leftover(n) for n in ("nzela-e2e.ab", "tmp_x", "robolectric1", "mine")],
                         [True, True, False, False])


class AgentDirectory(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.chmod(self.root, 0o700)
        patch = mock.patch.object(config, "tmp_root", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_directory_is_private_and_under_the_root(self):
        env = config.with_agent_tmpdir("app-data", {})
        mode = stat.S_IMODE(os.stat(env["TMPDIR"]).st_mode)
        self.assertEqual((os.path.dirname(env["TMPDIR"]), mode), (self.root, 0o700))


class RunnerUnits(unittest.TestCase):
    def test_each_runner_instance_gets_its_own_directory_made_before_it_starts(self):
        text = tmpdirs.runner_dropin("/t")
        self.assertIn("ExecStartPre=/bin/mkdir -p /t/runner-%i", text)
        self.assertIn('Environment="TMPDIR=/t/runner-%i"', text)
        self.assertIn('Environment="JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=/t/runner-%i"', text)

    def test_the_drop_in_is_written_once_and_a_second_sync_changes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            reloads = []
            first = units.sync_runner_tmp(home, "/t", lambda: reloads.append(1))
            second = units.sync_runner_tmp(home, "/t", lambda: reloads.append(1))
            path = os.path.join(home, ".config", "systemd", "user", "nzela-runner@.service.d", "50-tmp.conf")
            self.assertEqual((first, second, len(reloads)), (True, False, 1))
            with open(path) as f:
                self.assertEqual(f.read(), tmpdirs.runner_dropin("/t"))

    def test_a_changed_root_rewrites_the_drop_in(self):
        with tempfile.TemporaryDirectory() as home:
            units.sync_runner_tmp(home, "/t", lambda: None)
            self.assertTrue(units.sync_runner_tmp(home, "/u", lambda: None))


if __name__ == "__main__":
    unittest.main()
