import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from nzfleet import batch, cli, config, janitor, machine, state, tick


class SpawnedBatch(unittest.TestCase):
    def spawn(self, bid="manual-1"):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(state, "HOME", home), mock.patch.object(config, "HOME", home), \
                mock.patch.object(machine, "scoped", side_effect=lambda kind, argv: argv), \
                mock.patch.dict(config.FILE, {"NZFLEET_REPO": "/from/the/file"}), \
                mock.patch.dict(os.environ, {"NZFLEET_REPO": "/from/this/shell"}), \
                mock.patch.object(subprocess, "Popen") as popen:
            batch._spawn(bid)
            log = batch.log_of(bid)
        return popen.call_args, log

    def test_it_runs_the_batch_by_id_in_a_session_of_its_own(self):
        call, _ = self.spawn("manual-7")
        self.assertEqual(call.args[0][2:], ["nzfleet", "run-batch", "manual-7"])
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_the_machines_env_file_wins_over_the_calling_shell(self):
        call, _ = self.spawn()
        self.assertEqual(call.kwargs["env"]["NZFLEET_REPO"], "/from/the/file")

    def test_a_batch_gets_private_temporary_storage_inside_the_fleet_home(self):
        call, log = self.spawn()
        self.assertEqual(call.kwargs["env"]["TMPDIR"], os.path.join(os.path.dirname(os.path.dirname(log)), "tmp"))

    def test_it_writes_to_a_log_named_for_the_batch(self):
        call, log = self.spawn("manual-7")
        self.assertEqual(call.kwargs["stdout"].name, log)
        self.assertTrue(log.endswith("batch-manual-7.log"))


class TidySchedule(unittest.TestCase):
    def run_tidy(self, last, dry_run=False):
        with mock.patch.object(state, "read", return_value={**state.EMPTY, "tidied": last}), \
                mock.patch.object(state, "change"), \
                mock.patch.object(janitor, "run", return_value=["tidied /x: why"]) as run, \
                mock.patch.object(tick, "say") as say:
            tick.tidy_machine(dry_run)
        return run, say

    def test_a_pass_that_ran_five_minutes_ago_is_not_repeated(self):
        run, _ = self.run_tidy(time.time() - 300)
        run.assert_not_called()

    def test_a_pass_that_ran_twenty_minutes_ago_runs_and_logs_what_it_did(self):
        run, say = self.run_tidy(time.time() - 1200)
        run.assert_called_once()
        say.assert_called_once_with("tidied /x: why")

    def test_a_dry_run_ignores_the_schedule(self):
        run, _ = self.run_tidy(time.time(), dry_run=True)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
