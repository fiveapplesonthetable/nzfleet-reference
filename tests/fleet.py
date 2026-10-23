"""A state directory, a real git worktree and a fake agent process, for tests of what the daemon does with a run that ends."""

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from nzfleet import agents, blocked, config, machine, mail, state, tick, world

NOW = 1_800_000_000.0


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Fleet(unittest.TestCase):
    """Agents are started through the real `agents.start`; the process it launches is recorded and is already dead."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = os.path.realpath(directory.name)
        self.work = os.path.join(self.home, "work")
        self.launched, self.mails, self.said = [], [], []
        for patch in (
                mock.patch.object(state, "HOME", self.home),
                mock.patch.object(state, "PATH", os.path.join(self.home, "state.json")),
                mock.patch.object(state, "LOCK", os.path.join(self.home, "state.lock")),
                mock.patch.dict(os.environ, {blocked.LIVE: "1"}),
                mock.patch.object(config, "AGENT_SLOTS", 4),
                mock.patch.object(config, "refresh"),
                mock.patch.object(agents, "LOGS", os.path.join(self.home, "agents")),
                mock.patch.object(agents, "WORK", self.work),
                mock.patch.object(agents, "COMMAND", "agent {model} {brief}"),
                mock.patch.object(agents, "subprocess", types.SimpleNamespace(Popen=self.popen, STDOUT=subprocess.STDOUT)),
                mock.patch.object(machine, "scoped", side_effect=lambda kind, argv, **kw: argv),
                mock.patch.object(world, "comment", side_effect=lambda issue, body: self.said.append(body)),
                mock.patch.object(mail, "send", side_effect=lambda subject, body: self.mails.append((subject, body)) or True),
                mock.patch.object(tick, "tell_news", side_effect=self.said.append),
                mock.patch.object(tick, "tell_event", side_effect=self.said.append)):
            patch.start()
            self.addCleanup(patch.stop)
        self.wt = self.worktree("app-data")

    def popen(self, argv, **kw):
        self.launched.append((argv, kw))
        return types.SimpleNamespace(pid=2 ** 22 + len(self.launched))

    def worktree(self, slug):
        """A worktree with one commit above its base and one uncommitted file, recorded the way `nzfleet start` records it."""
        path = os.path.join(self.work, slug)
        os.makedirs(path)
        sh(path, "git", "init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            sh(path, "git", "config", key, value)
        with open(os.path.join(path, "a.txt"), "w") as output:
            output.write("a\n")
        sh(path, "git", "add", "-A")
        sh(path, "git", "commit", "-qm", "base")
        base = sh(path, "git", "rev-parse", "HEAD")
        with open(os.path.join(path, "b.txt"), "w") as output:
            output.write("b\n")
        sh(path, "git", "add", "-A")
        sh(path, "git", "commit", "-qm", "The store reads from disk")
        with open(os.path.join(path, "c.txt"), "w") as output:
            output.write("c\n")
        with state.change() as s:
            s["worktrees"][slug] = {"path": path, "base": base, "base_pr": None, "port": 20000, "agent": "app-data"}
        return path

    def start(self, key="app-data", task="build the store", area="app"):
        agents.start(key, task, area=area)

    def die(self, key, at, log=""):
        """The run's process is gone and its log holds `log`; reaps it at time `at`."""
        with state.change() as s:
            with open(s["agents"][key]["log"], "w") as output:
                output.write(log)
        with mock.patch.object(agents.time, "time", return_value=at):
            agents.reap()

    def agents(self):
        return state.read()["agents"]
