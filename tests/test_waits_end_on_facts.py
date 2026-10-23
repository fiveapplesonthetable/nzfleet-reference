"""Waits end on a fact: the thing answered, or the thing they wait for is gone. Never on a count of seconds."""

import itertools
import signal
import subprocess
import unittest
from unittest import mock

from nzfleet import areas, db, procs, prove
from nzfleet.world import Failed

SLOW = 500  # polls, far past any count a wait could have been given


class Clock:
    """A clock that moves only when something sleeps, so a deadline in the code under test is reached by polling, not by waiting."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class SlowPreview(unittest.TestCase):
    def preview(self, refusals, alive):
        answers = itertools.chain([111] * refusals, itertools.repeat(0))
        sock = mock.MagicMock()
        sock.__enter__.return_value.connect_ex.side_effect = lambda _: next(answers)
        said = []
        clock = Clock()
        with mock.patch.dict(prove.os.environ, {"NZ_PORT": "5999"}), \
             mock.patch.object(prove.procs, "start", return_value=""), \
             mock.patch.object(prove.procs, "_pid", return_value=77, create=True), \
             mock.patch.object(prove.procs, "alive", side_effect=alive), \
             mock.patch("socket.socket", return_value=sock), \
             mock.patch.object(prove.time, "sleep", side_effect=clock.sleep):
            prove._preview("/wt", said.append)
        return said

    def test_a_preview_slower_than_any_count_is_waited_for(self):
        self.assertEqual(self.preview(SLOW, lambda pid: True), [])

    def test_a_preview_that_exits_is_named_instead_of_waited_for(self):
        polls = itertools.chain([True] * 3, itertools.repeat(False))
        said = self.preview(SLOW, lambda pid: next(polls))
        self.assertEqual(len(said), 1)
        self.assertIn("exited", said[0])
        self.assertIn(".nzrun/preview.log", said[0])


class SlowDatabase(unittest.TestCase):
    def docker(self, health_polls, running=True):
        health = itertools.chain(["starting"] * health_polls, itertools.repeat("healthy"))

        def docker(*args):
            fmt = args[args.index("-f") + 1]
            out = "true" if "State.Running" in fmt and running else "false" if "State.Running" in fmt else next(health)
            return subprocess.CompletedProcess(args, 0, out + "\n", "")
        return docker

    def test_a_service_slower_than_any_count_is_waited_for(self):
        with mock.patch.object(areas.time, "sleep"):
            self.assertTrue(areas._healthy(self.docker(SLOW), "svc"))

    def test_a_service_that_stopped_is_reported_not_waited_for(self):
        with mock.patch.object(areas.time, "sleep"):
            self.assertFalse(areas._healthy(self.docker(SLOW, running=False), "svc"))

    def up(self, refusals, running):
        answers = itertools.chain([1] * refusals, itertools.repeat(0))
        ran = []

        def run(argv, **_):
            ran.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, "true\n" if running else "false\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(db.subprocess, "run", side_effect=run), \
             mock.patch.object(db, "_psql", side_effect=lambda *a: subprocess.CompletedProcess([], next(answers), "", "")), \
             mock.patch.object(db.glob, "glob", return_value=[]), \
             mock.patch.object(db.docker_budget, "container_flags", return_value=[]), \
             mock.patch.object(db.time, "sleep"):
            return db.up("slow", "/checkout", 58433)

    def test_a_postgres_slower_than_any_count_is_waited_for(self):
        self.assertTrue(self.up(SLOW, running=True).endswith("/nzela"))

    def test_a_postgres_that_stopped_is_named_not_waited_for(self):
        with self.assertRaises(Failed) as stopped:
            self.up(SLOW, running=False)
        self.assertIn("stopped", str(stopped.exception))
        self.assertIn("docker logs agentdb-slow", str(stopped.exception))


class Termination(unittest.TestCase):
    def kill(self, exits_after_signal, **kwargs):
        sent = []
        with mock.patch.object(procs.os, "killpg", side_effect=lambda pid, sig: sent.append(sig)), \
             mock.patch.object(procs, "alive", return_value=not exits_after_signal), \
             mock.patch.object(procs.time, "sleep", side_effect=AssertionError("kill_group waited")):
            return sent, procs.kill_group(4242, **kwargs)

    def test_a_group_that_takes_long_to_say_what_it_ran_is_not_killed(self):
        self.assertEqual(self.kill(False), ([signal.SIGTERM], False))

    def test_now_is_the_only_way_to_send_sigkill(self):
        self.assertEqual(self.kill(True, now=True), ([signal.SIGKILL], True))

    def test_a_leader_that_ignores_sigterm_is_reported_not_waited_for(self):
        self.assertFalse(self.kill(False)[1])

    def test_a_group_that_is_already_gone_counts_as_exited(self):
        with mock.patch.object(procs.os, "killpg", side_effect=ProcessLookupError):
            self.assertTrue(procs.kill_group(4242))

    def test_kill_names_the_process_that_ignored_sigterm(self):
        with mock.patch.object(procs, "_pid", return_value=4242), \
             mock.patch.object(procs, "alive", return_value=True), \
             mock.patch.object(procs.os, "readlink", return_value="/wt"), \
             mock.patch.object(procs.os, "getpgid", return_value=4242), \
             mock.patch.object(procs.plan, "owns", return_value=True), \
             mock.patch.object(procs, "kill_group", return_value=False), \
             mock.patch.object(procs, "wait_for", return_value=3):
            why = procs.kill("agent", "/wt")
        self.assertIn("pid 4242", why)
        self.assertIn("nzfleet kill agent --now", why)


class TimeoutThatIgnoresSigterm(unittest.TestCase):
    def test_a_check_past_its_ceiling_is_killed_with_sigkill(self):
        proc = mock.Mock(pid=77)
        proc.poll.return_value = None
        with mock.patch.object(prove.subprocess, "Popen", return_value=proc), \
             mock.patch.object(prove.procs, "wait_for", return_value=3), \
             mock.patch.object(prove.procs, "kill_group") as stop, \
             mock.patch("builtins.open", mock.mock_open()):
            code = prove.run_check(["check"], "/wt", "/log", 1, lambda _: None)
        self.assertEqual(code, 124)
        stop.assert_called_once_with(77, now=True)


if __name__ == "__main__":
    unittest.main()
