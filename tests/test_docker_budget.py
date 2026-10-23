"""The selected Docker daemon places native jobs under the owner quota."""

import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from nzfleet import docker_budget


class DockerBudget(unittest.TestCase):
    def test_native_root_docker_inherits_the_owner_quota(self):
        flags = docker_budget.flags("unix:///var/run/docker.sock", "systemd", [], "2400000 100000", 4, 24, strict=True)
        self.assertEqual(flags, ["--cgroup-parent=user-1000.slice", "--cpus=4"])

    def test_a_native_daemon_without_a_bounded_parent_is_refused(self):
        for cpu_max in ("max 100000", "2500000 100000", ""):
            with self.subTest(cpu_max=cpu_max), self.assertRaisesRegex(ValueError, "quota"):
                docker_budget.flags("unix:///var/run/docker.sock", "systemd", [], cpu_max, 4, 24, strict=True)

    def test_native_cgroupfs_driver_is_refused(self):
        with self.assertRaisesRegex(ValueError, "systemd cgroup driver"):
            docker_budget.flags("unix:///var/run/docker.sock", "cgroupfs", [], "2400000 100000", 4, 24, strict=True)

    def test_rootless_and_hosted_docker_get_portable_limits(self):
        for endpoint, security in (("unix:///run/user/1001/docker.sock", ["name=rootless"]),
                                   ("tcp://host:2375", [])):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(docker_budget.flags(endpoint, "systemd", security, "", 2, 24), ["--cpus=2"])

    def test_hosted_rootful_default_socket_needs_no_owner_unit(self):
        self.assertEqual(docker_budget.flags("unix:///var/run/docker.sock", "cgroupfs", [], "", 2, 0,
                                             strict=False), ["--cpus=2"])

    def test_managed_local_socket_alias_cannot_bypass_the_owner_quota(self):
        with self.assertRaisesRegex(ValueError, "quota"):
            docker_budget.flags("unix:///run/docker.sock", "systemd", [], "", 2, 24, strict=True)

    def test_malformed_docker_info_is_a_value_error(self):
        for driver, security in ((None, []), ("systemd", None), ("systemd", [None])):
            with self.subTest(driver=driver, security=security), self.assertRaises(ValueError):
                docker_budget.flags("unix:///run/docker.sock", driver, security, "", 2, 24)

    def test_portable_default_socket_does_not_read_the_owner_unit(self):
        def docker(argv, **_):
            answer = ('"systemd" ["name=apparmor"]' if argv[1] == "info" else "unix:///var/run/docker.sock")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with patch.dict("os.environ", {"DOCKER_HOST": ""}):
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=Path("/missing/cpu.max")),
                             ["--cpus=2"])
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=Path("/missing/cpu.max"),
                                                           prefer_owner=True), ["--cpus=2"])

    def test_managed_default_socket_without_an_owner_unit_refuses(self):
        def docker(argv, **_):
            answer = ('"systemd" []' if argv[1] == "info" else "unix:///var/run/docker.sock")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with patch.dict("os.environ", {"DOCKER_HOST": ""}), self.assertRaisesRegex(ValueError, "quota"):
            docker_budget.container_flags(2, run=docker, cgroup=Path("/missing/cpu.max"), strict=True)

    def test_pre_push_uses_a_valid_local_owner_parent(self):
        def docker(argv, **_):
            answer = ('"systemd" []' if argv[1] == "info" else "unix:///run/docker.sock")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"DOCKER_HOST": ""}):
            parent = Path(directory) / "cpu.max"
            parent.write_text("2400000 100000")
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=parent, prefer_owner=True),
                             ["--cgroup-parent=user-1000.slice", "--cpus=2"])

    def test_portable_user_1000_with_an_unlimited_slice_keeps_the_cpu_cap(self):
        def docker(argv, **_):
            answer = ('"systemd" []' if argv[1] == "info" else "unix:///run/docker.sock")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"DOCKER_HOST": ""}):
            parent = Path(directory) / "cpu.max"
            parent.write_text("max 100000")
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=parent, prefer_owner=True),
                             ["--cpus=2"])

    def test_managed_job_on_a_remote_daemon_never_passes_a_local_parent(self):
        def docker(argv, **_):
            answer = ('"systemd" []' if argv[1] == "info" else "tcp://builder.example:2375")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with patch.dict("os.environ", {"DOCKER_HOST": ""}):
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=Path("/missing/cpu.max"),
                                                           strict=True), ["--cpus=2"])

    def test_selected_native_daemon_and_cgroup_file_drive_the_flags(self):
        calls = []

        def docker(argv, **_):
            calls.append(argv)
            answer = ('"systemd" ["name=apparmor"]' if argv[1] == "info" else "unix:///var/run/docker.sock")
            return subprocess.CompletedProcess(argv, 0, answer, "")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"DOCKER_HOST": ""}):
            parent = Path(directory) / "cpu.max"
            parent.write_text("2400000 100000")
            self.assertEqual(docker_budget.container_flags(2, run=docker, cgroup=parent, strict=True),
                             ["--cgroup-parent=user-1000.slice", "--cpus=2"])
        self.assertEqual([argv[1] for argv in calls], ["info", "context"])
