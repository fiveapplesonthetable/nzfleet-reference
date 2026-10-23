import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin/root"))
import runner_shares


class RegisteredRunnersSplitTheirAccountQuota(unittest.TestCase):
    def test_counts_follow_the_configured_slice_quota_and_runner_count(self):
        self.assertEqual(runner_shares.shares(36, ["four", "one", "three", "two"]),
                         {"four": 9, "one": 9, "three": 9, "two": 9})
        self.assertEqual(sum(runner_shares.shares(8, ["a", "b", "c"]).values()), 8)

    def test_more_runners_than_cpus_is_refused(self):
        with self.assertRaisesRegex(ValueError, "runner count"):
            runner_shares.shares(1, ["a", "b"])

    def test_runner_units_get_their_own_quota_dropins(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            for name in ("one", "two"):
                runner = home / "actions-runner" / name
                runner.mkdir(parents=True)
                (runner / ".runner").touch()
            with patch.object(runner_shares.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=root)), \
                 patch.object(runner_shares.os, "geteuid", return_value=1001), \
                 patch.object(runner_shares, "quota", return_value=6):
                runner_shares.install(1001, "user-1001.slice.conf")
            for name in ("one", "two"):
                dropin = home / ".config/systemd/user" / f"nzela-runner@{name}.service.d/50-cpu-share.conf"
                self.assertEqual(dropin.read_text(), "[Service]\nCPUQuota=300%\n")
