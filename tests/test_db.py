"""A native agent database stays inside the selected Docker CPU budget."""

import subprocess
import unittest
from unittest.mock import patch

from nzfleet import db
from nzfleet.world import Failed


class NativeDatabaseBudget(unittest.TestCase):
    def start(self, flags, *, managed):
        calls = []

        def docker(argv, **_):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.dict(db.os.environ, {"NZELA_MANAGED_DOCKER": "1" if managed else "0"}), \
             patch.object(db.subprocess, "run", side_effect=docker), \
             patch.object(db, "_psql", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(db.glob, "glob", return_value=[]), \
             patch.object(db.docker_budget, "container_flags", return_value=flags) as budget:
            url = db.up("native", "/checkout", 58432)
        return calls, budget, url

    def test_native_database_inherits_the_owner_parent_and_two_cpu_cap(self):
        calls, budget, url = self.start(["--cgroup-parent=user-1000.slice", "--cpus=2"], managed=True)
        budget.assert_called_once_with(2, strict=True, prefer_owner=False)
        self.assertEqual(calls[0], ["docker", "rm", "-f", "agentdb-native"])
        run = calls[1]
        self.assertEqual(run[:8], ["docker", "run", "-d", "--init", "--cgroup-parent=user-1000.slice",
                                    "--cpus=2", "--name", "agentdb-native"])
        self.assertEqual(run[8:14], ["-p", "127.0.0.1:58432:5432", "--tmpfs",
                                     "/var/lib/postgresql/data:rw,size=4g", "-e", "POSTGRES_PASSWORD=nzela"])
        self.assertEqual(url, "postgres://postgres:nzela@127.0.0.1:58432/nzela")

    def test_portable_database_uses_the_two_cpu_cap_without_a_parent(self):
        calls, budget, _ = self.start(["--cpus=2"], managed=False)
        budget.assert_called_once_with(2, strict=False, prefer_owner=True)
        self.assertEqual(calls[1][4:7], ["--cpus=2", "--name", "agentdb-native"])
        self.assertFalse(any(arg.startswith("--cgroup-parent") for arg in calls[1]))

    def test_invalid_managed_budget_prevents_replacement_or_launch(self):
        with patch.dict(db.os.environ, {"NZELA_MANAGED_DOCKER": "1"}), \
             patch.object(db.docker_budget, "container_flags", side_effect=ValueError("owner quota missing")), \
             patch.object(db.subprocess, "run") as docker:
            with self.assertRaisesRegex(Failed, "Docker budget: owner quota missing"):
                db.up("native", "/checkout", 58432)
        docker.assert_not_called()
