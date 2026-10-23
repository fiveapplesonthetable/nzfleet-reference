import os
import subprocess
import sys
import tempfile
import unittest

import live_home

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def import_suite(env_file: str, **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if not k.startswith("NZFLEET_")}
    full.update(NZFLEET_ENV=env_file, **env)
    return subprocess.run([sys.executable, "-c", "import tests"], cwd=TOOLS, env=full, capture_output=True, text=True)


class TheSuiteAndTheLiveHome(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.mkdtemp()
        self.live = os.path.join(scratch, "live")
        self.env_file = os.path.join(scratch, "env")
        with open(self.env_file, "w") as f:
            f.write(f"NZFLEET_HOME={self.live}\n")

    def test_importing_against_the_home_the_env_file_names_is_refused(self):
        done = import_suite(self.env_file)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("LiveHomeRefused", done.stderr)

    def test_a_temp_home_imports(self):
        done = import_suite(self.env_file, NZFLEET_HOME=tempfile.mkdtemp())
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_live_flag_allows_the_live_home(self):
        done = import_suite(self.env_file, NZFLEET_LIVE="1")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_default_home_is_live_even_with_no_env_file(self):
        with self.assertRaises(live_home.LiveHomeRefused):
            live_home.check(os.path.expanduser("~/.nzfleet"), {}, {})
