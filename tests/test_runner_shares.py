import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin/root"))
import runner_shares


class EachRunnerMayUseTheHeaviestJob(unittest.TestCase):
    def test_a_runner_gets_the_heaviest_job_not_a_fraction_of_the_slice(self):
        names = [str(n) for n in range(8)]
        self.assertEqual(set(runner_shares.shares(20, names, 12).values()), {12})
        self.assertEqual(set(runner_shares.shares(36, names, 12).values()), {12})

    def test_a_slice_smaller_than_the_heaviest_job_bounds_the_runner(self):
        self.assertEqual(runner_shares.shares(8, ["a", "b"], 12), {"a": 8, "b": 8})

    def test_no_runner_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no registered runner"):
            runner_shares.shares(8, [], 12)

    def test_runner_units_get_their_own_quota_dropins(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            for name in ("one", "two"):
                runner = home / "actions-runner" / name
                runner.mkdir(parents=True)
                (runner / ".runner").touch()
            with patch.object(runner_shares.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=root)), \
                 patch.object(runner_shares.os, "geteuid", return_value=1001), \
                 patch.object(runner_shares, "quota", return_value=6), \
                 patch.object(runner_shares, "heaviest_job", return_value=3):
                runner_shares.install(1001, "user-1001.slice.conf")
            for name in ("one", "two"):
                dropin = home / ".config/systemd/user" / f"nzela-runner@{name}.service.d/50-cpu-share.conf"
                self.assertEqual(dropin.read_text(), "[Service]\nCPUQuota=300%\n")


    def test_each_runner_writes_temporary_files_under_its_accounts_own_directory(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            runner = home / "actions-runner" / "one"
            runner.mkdir(parents=True)
            (runner / ".runner").touch()
            account = SimpleNamespace(pw_dir=root, pw_name="nzela-ci")
            with patch.object(runner_shares.pwd, "getpwuid", return_value=account), \
                 patch.object(runner_shares.os, "geteuid", return_value=1001), \
                 patch.object(runner_shares, "quota", return_value=6), \
                 patch.object(runner_shares, "heaviest_job", return_value=3):
                runner_shares.install(1001, "user-1001.slice.conf", tmp_root="/srv/fleet/tmp")
            text = (home / ".config/systemd/user/nzela-runner@one.service.d/50-tmp.conf").read_text()
            self.assertIn('Environment="TMPDIR=/srv/fleet/tmp/nzela-ci/runner-one"', text)
            self.assertIn("-Djava.io.tmpdir=/srv/fleet/tmp/nzela-ci/runner-one", text)
