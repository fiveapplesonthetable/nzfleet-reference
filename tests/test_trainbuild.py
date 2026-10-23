import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nzfleet import trainbuild


class RedrawOutput(unittest.TestCase):
    def test_a_failed_console_redraw_keeps_its_full_log_and_reports_the_tail(self):
        with tempfile.TemporaryDirectory() as tree:
            def fail(command, shell, cwd, env, stdout, stderr):
                stdout.write("screen 112 passed\nscreen 113 failed\n")
                return subprocess.CompletedProcess(command, 1)

            with mock.patch.object(trainbuild.batch, "CODE", tree), mock.patch.object(subprocess, "run", side_effect=fail):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    trainbuild.redraw(tree, ["console"])

            path = os.path.join(tree, trainbuild.SHOT_LOG)
            self.assertEqual(Path(path).read_text(), f"$ {[os.path.join(tree, 'bin', 'redraw-console.sh'), tree]}\nscreen 112 passed\nscreen 113 failed\n")
            self.assertIn(path, failure.exception.output)
            self.assertIn("screen 113 failed", failure.exception.output)


class AndroidRedraw(unittest.TestCase):
    def test_android_screenshots_are_recorded_even_when_gradle_cached_the_test(self):
        with tempfile.TemporaryDirectory() as tree, mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess("gradle", 0)
            trainbuild.redraw(tree, ["android"])

        command = run.call_args.args[0]
        self.assertIn("--rerun", command)
        self.assertIn("-Proborazzi.test.record=true", command)


if __name__ == "__main__":
    unittest.main()
