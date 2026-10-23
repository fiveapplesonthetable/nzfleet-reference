"""A run's processes live in a systemd scope named for it, and ending the run ends the scope."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fleet import Fleet
from nzfleet import agents, dispatch, machine, state, tick

HAS_USER_MANAGER = bool(shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"))


class UnitNames(unittest.TestCase):
    def test_a_unit_name_is_safe_for_systemd_and_unique_per_start(self):
        first = machine.agent_unit("fix-3~2/x", 1_800_000_000.0)
        self.assertRegex(first, r"^nzf-agent-[A-Za-z0-9_-]+-1800000000$")
        self.assertNotEqual(first, machine.agent_unit("fix-3~2/x", 1_800_000_001.0))

    def test_a_scope_with_a_unit_is_named_in_the_command(self):
        with mock.patch("shutil.which", return_value="/usr/bin/systemd-run"), mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            argv = machine.scoped("agent", ["x"], unit="nzf-agent-a-1")
        self.assertIn("--unit=nzf-agent-a-1", argv)

    def test_a_live_pid_matches_its_process_start(self):
        process_start = agents._process_start(os.getpid())
        self.assertIsNotNone(process_start)
        self.assertTrue(agents._alive(os.getpid(), process_start))
        self.assertFalse(agents._alive(os.getpid(), "a different process start"))


class Sessions(unittest.TestCase):
    def test_a_quoted_command_path_is_still_recognised_as_claude(self):
        self.assertTrue(dispatch.has_sessions("'/opt/my tools/claude' -p {brief}"))
        self.assertFalse(dispatch.has_sessions("'/opt/claude tools/codex' exec {brief}"))


class RecordedUnit(Fleet):
    def test_a_started_run_records_the_unit_it_runs_in(self):
        self.start()
        record = self.agents()["app-data"]
        self.assertEqual(record["unit"], machine.agent_unit("app-data", record["started"]))

    def test_reaping_a_run_stops_its_unit(self):
        self.start()
        unit = self.agents()["app-data"]["unit"]
        with mock.patch.object(machine, "stop_unit") as stop:
            self.die("app-data", 5, "PR #1\n")
        stop.assert_called_once_with(unit)

    def test_a_run_recorded_without_a_unit_is_reaped_as_before(self):
        self.start()
        with state.change() as s:
            del s["agents"]["app-data"]["unit"]
        with mock.patch.object(machine, "stop_unit") as stop:
            self.die("app-data", 5, "PR #1\n")
        stop.assert_not_called()
        self.assertEqual(self.agents()["app-data"]["status"], "done")


@unittest.skipUnless(HAS_USER_MANAGER, "needs a systemd user manager, which a ci container does not have")
class StrayProcesses(unittest.TestCase):
    def test_a_process_the_agent_backgrounds_dies_when_the_run_is_reaped(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(state, "PATH", os.path.join(home, "state.json")), \
                mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")):
            pidfile, log = os.path.join(home, "stray.pid"), os.path.join(home, "a.log")
            unit = machine.agent_unit("stray-test", time.time())
            self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
            script = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}"
            proc = subprocess.Popen(machine.scoped("agent", ["sh", "-c", script], unit=unit), start_new_session=True)
            proc.wait()
            with open(pidfile) as f:
                stray = int(f.read())
            self.assertTrue(agents._alive(stray))
            with open(log, "w") as f:
                f.write("PR #1\n")
            with state.change() as s:
                s["agents"]["stray-test"] = {"status": "running", "pid": proc.pid, "unit": unit, "task": "t", "model": "sonnet", "log": log, "started": 0}
            agents.reap()
            self.assertFalse(agents._alive(stray))

    def test_a_reused_agent_pid_does_not_keep_its_scope_alive(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(state, "PATH", os.path.join(home, "state.json")), \
                mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")):
            pidfile, log = os.path.join(home, "stray.pid"), os.path.join(home, "a.log")
            unit = machine.agent_unit("reused-pid-test", time.time())
            self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
            script = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}"
            proc = subprocess.Popen(machine.scoped("agent", ["sh", "-c", script], unit=unit), start_new_session=True)
            proc.wait()
            with open(pidfile) as f:
                stray = int(f.read())
            self.assertTrue(agents._alive(stray))
            with open(log, "w") as f:
                f.write("PR #1\n")
            with state.change() as s:
                s["agents"]["reused-pid-test"] = {
                    "status": "running", "pid": os.getpid(), "process_start": "an earlier process",
                    "unit": unit, "task": "t", "model": "sonnet", "log": log, "started": 0,
                }
            agents.reap()
            self.assertFalse(agents._alive(stray))

    def test_a_process_the_agent_backgrounds_dies_when_the_run_is_retired(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(state, "PATH", os.path.join(home, "state.json")), \
                mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")):
            pidfile = os.path.join(home, "stray.pid")
            unit = machine.agent_unit("retired-test", time.time())
            self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
            script = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}"
            proc = subprocess.Popen(machine.scoped("agent", ["sh", "-c", script], unit=unit), start_new_session=True)
            proc.wait()
            with open(pidfile) as f:
                stray = int(f.read())
            self.assertTrue(agents._alive(stray))
            with state.change() as s:
                s["agents"]["hands-1"] = {"status": "running", "pid": proc.pid, "unit": unit, "task": "t"}
            tick.retire_rebase_work({1})
            self.assertEqual(state.read()["agents"]["hands-1"]["status"], "retired")
            self.assertFalse(agents._alive(stray))


if __name__ == "__main__":
    unittest.main()
