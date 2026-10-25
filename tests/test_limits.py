import os
import subprocess
import tempfile
import unittest

from nzfleet import limits

CONFIG = "  retries: 0\n  timeout: 120_000\n"


def git(work, *args):
    subprocess.run(["git", "-C", work, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class Limits(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        git(self.work, "init", "-q", "-b", "main")
        self.write("playwright.config.ts", CONFIG)
        self.write("budget.mjs", "const BUDGET_BYTES = 245 * 1024\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "base")
        self.base = subprocess.run(["git", "-C", self.work, "rev-parse", "HEAD"], capture_output=True,
                                   text=True).stdout.strip()

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w") as out:
            out.write(text)

    def verdict(self, name, text):
        self.write(name, text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "change")
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            diff = subprocess.run(["git", "diff", "-U0", "--no-color", f"{self.base}...HEAD"],
                                  capture_output=True, text=True).stdout
            return limits.refusals(diff)
        finally:
            os.chdir(cwd)

    def test_an_unrelated_change_is_let_through(self):
        self.assertEqual(self.verdict("playwright.config.ts", CONFIG + "  workers: 2\n"), [])

    def test_a_longer_timeout_is_refused_and_named(self):
        said = self.verdict("playwright.config.ts", "  retries: 0\n  timeout: 180_000\n")
        self.assertEqual(said, ["playwright.config.ts: timeout raised from 120000 to 180000"])

    def test_a_shorter_timeout_is_let_through(self):
        self.assertEqual(self.verdict("playwright.config.ts", "  retries: 0\n  timeout: 60_000\n"), [])

    def test_a_larger_size_budget_is_refused(self):
        self.assertTrue(self.verdict("budget.mjs", "const BUDGET_BYTES = 260 * 1024\n"))

    def test_a_retry_is_refused(self):
        self.assertTrue(self.verdict("playwright.config.ts", "  retries: 2\n  timeout: 120_000\n"))

    def test_serial_mode_is_refused(self):
        self.assertTrue(self.verdict("a.spec.ts", "test.describe.configure({ mode: 'serial' })\n"))

    def test_test_slow_is_refused(self):
        self.assertTrue(self.verdict("a.spec.ts", "test.slow()\n"))

    def test_a_missing_base_is_refused(self):
        self.assertEqual(limits.main(""), 1)

    def test_a_base_that_is_not_a_commit_is_refused(self):
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            self.assertEqual(limits.main("nonesuch"), 1)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
