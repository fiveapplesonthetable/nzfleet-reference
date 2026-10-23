import io
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from nzfleet import areas, plan


EXHAUSTED = ("Error response from daemon: all predefined address pools have been fully subnetted: "
             "could not find an available, non-overlapping IPv4 address pool among the defaults to assign to the network")


class FakeDocker:
    """Answers `docker` calls from a set of networks; `vanish_when` lets a test pull every network out from under the job."""

    def __init__(self, vanish_when=None, create_error="", existing=0):
        self.networks, self.calls = set(), []
        self.vanish_when = vanish_when
        self.create_error = create_error
        self.existing = [f"leaked-{i}" for i in range(existing)]

    def __call__(self, argv, **_):
        self.calls.append(argv)
        verb = argv[1:3]
        if verb == ["network", "create"]:
            if self.create_error:
                return subprocess.CompletedProcess(argv, 1, "", self.create_error)
            self.networks.add(argv[3])
        elif verb == ["network", "ls"]:
            return subprocess.CompletedProcess(argv, 0, "".join(f"{n}\n" for n in self.existing), "")
        elif verb == ["network", "rm"]:
            self.networks.discard(argv[3])
        elif verb == ["network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if argv[3] in self.networks else 1, "", "")
        elif argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "true\n" if "State.Running" in argv[3] else "healthy\n", "")
        elif argv[1] == "run" and self.vanish_when and self.vanish_when(argv):
            self.networks.clear()
        elif argv[1] == "run" and "--rm" in argv:
            net = argv[argv.index("--network") + 1]
            return subprocess.CompletedProcess(argv, 0 if net in self.networks else 125, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self):
        return [" ".join(c[1:3]) if c[1] == "network" else c[1] for c in self.calls]


def run(docker, service=True, out=None):
    job = plan.Job("rust", "img", {"name": "postgres", "image": "pg"} if service else None, {}, "", ("true",))
    with tempfile.TemporaryDirectory() as target:
        with patch.object(areas.docker_budget, "container_flags", return_value=["--cpus=2"]):
            return areas.run_in_image(job, "net", "/w", {}, target, out or io.BytesIO(), run=docker)


class ANetworkIsThereWhenTheJobRuns(unittest.TestCase):
    def test_the_job_and_service_inherit_the_native_parent(self):
        docker = FakeDocker()
        job = plan.Job("rust", "img", {"name": "postgres", "image": "pg"}, {}, "", ("true",))
        with tempfile.TemporaryDirectory() as target, \
             patch.object(areas.docker_budget, "container_flags", return_value=["--cgroup-parent=user-1000.slice", "--cpus=2"]):
            self.assertEqual(areas.run_in_image(job, "net", "/w", {}, target, io.BytesIO(), run=docker), 0)
        runs = [call for call in docker.calls if call[:2] == ["docker", "run"]]
        self.assertEqual(len(runs), 2)
        for argv in runs:
            self.assertIn("--cgroup-parent=user-1000.slice", argv)

    def test_invalid_native_parent_stops_before_a_network_or_container_starts(self):
        docker = FakeDocker()
        job = plan.Job("rust", "img", None, {}, "", ("true",))
        with tempfile.TemporaryDirectory() as target, \
             patch.object(areas.docker_budget, "container_flags", side_effect=ValueError("owner quota missing")):
            self.assertEqual(areas.run_in_image(job, "net", "/w", {}, target, io.BytesIO(), run=docker), areas.NOT_STARTED)
        self.assertEqual(docker.calls, [])

    def test_a_job_without_a_service_creates_its_network_right_before_it_runs(self):
        docker = FakeDocker()
        run(docker, service=False)
        self.assertEqual(docker.verbs()[:3], ["network inspect", "network create", "run"])

    def test_a_network_removed_while_the_service_started_is_created_again(self):
        docker = FakeDocker(vanish_when=lambda argv: "-d" in argv)
        self.assertEqual(run(docker), 0)

    def test_the_network_is_removed_when_the_job_ends(self):
        docker = FakeDocker()
        run(docker)
        self.assertEqual(docker.networks, set())


class LogsOfALostNetwork(unittest.TestCase):
    def test_a_network_that_is_not_found_is_infrastructure(self):
        log = "docker: Error response from daemon: failed to set up container networking: network nzf-manual-1-r0-again-deploy not found"
        self.assertEqual(plan.classify(log), "infrastructure")

    def test_a_container_that_could_not_be_networked_is_infrastructure(self):
        self.assertEqual(plan.classify("failed to set up container networking: driver failed"), "infrastructure")


class ANetworkThatCannotBeCreated(unittest.TestCase):
    def run_exhausted(self, existing=31):
        out = io.BytesIO()
        code = run(FakeDocker(create_error=EXHAUSTED, existing=existing), out=out)
        return code, out.getvalue().decode()

    def test_the_job_is_not_started(self):
        docker = FakeDocker(create_error=EXHAUSTED)
        run(docker)
        self.assertNotIn("run", docker.verbs())

    def test_the_log_names_the_exhausted_pools_and_how_many_networks_exist(self):
        code, log = self.run_exhausted(existing=31)
        self.assertEqual(code, areas.NOT_STARTED)
        self.assertIn("address pools are exhausted", log)
        self.assertIn("31 networks", log)

    def test_the_log_is_infrastructure_so_the_batch_retries_and_the_pr_is_not_blamed(self):
        self.assertEqual(plan.classify(self.run_exhausted()[1]), "infrastructure")

    def test_a_failure_for_another_reason_is_infrastructure_too(self):
        out = io.BytesIO()
        run(FakeDocker(create_error="permission denied"), out=out)
        log = out.getvalue().decode()
        self.assertIn("permission denied", log)
        self.assertEqual(plan.classify(log), "infrastructure")


class LogsOfAnExhaustedDaemon(unittest.TestCase):
    def test_the_daemons_own_words_are_infrastructure(self):
        self.assertEqual(plan.classify(EXHAUSTED), "infrastructure")
