import io
import contextlib
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import tests
from tests.argv import environment, option, shell_script
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from nzfleet import areas, plan

CI = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".github", "workflows", "ci.yaml")


class TheEnvironmentAJobGetsForTheCachesMountedIntoIt(unittest.TestCase):
    def test_native_runner_maps_each_warm_job_into_its_retained_target_slot(self):
        class Meter:
            seconds = 0.0
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None

        with tempfile.TemporaryDirectory() as temp:
            target = os.path.join(temp, "target")
            cache = os.path.join(temp, "cache")
            captured = {}
            def launch(job, name, root, mounts, directory, out, cpus):
                captured[job.area] = (mounts.copy(), plan.job_argv(job, name, name, mounts, root,
                    f"{os.getuid()}:{os.getgid()}", {}))
                return 0
            args = SimpleNamespace(workflow=CI, worktree=os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")),
                target=target, cache=cache, main="abc123", areas=["rust", "compat", "e2e", "contract"],
                slot="", sccache="", sccache_bin="", stats="", name="probe")
            with patch.object(areas, "run_in_image", side_effect=launch), \
                 patch.object(areas, "CpuMeter", return_value=Meter()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(areas.run(args), 0)
            for area in args.areas:
                mounts, argv = captured[area]
                warm = os.path.join(target, area, "warm")
                self.assertEqual(mounts[warm], "/warm")
                self.assertTrue(os.path.isdir(warm))
                self.assertIn(f"{warm}:/warm", argv)
                self.assertIn(f"chown -R {os.getuid()}:{os.getgid()}", shell_script(argv))
                self.assertIn("/warm", shell_script(argv))

    def test_native_docker_launch_uses_each_reviewed_ci_cap(self):
        import yaml
        with open(CI) as source:
            workflow = yaml.safe_load(source)
        calls = []
        caps = []

        def run(argv, **_):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def flags(cpus, **_):
            caps.append(cpus)
            return [f"--cpus={cpus}"]

        inherited = {"CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_INCREMENTAL": "1"}
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, inherited, clear=True), \
             patch.object(areas.docker_budget, "container_flags", side_effect=flags), \
             patch.object(areas, "_healthy", return_value=True):
            for area in ("rust", "compat", "e2e", "contract", "console"):
                current = plan.job_for(workflow, area, {"NZELA_BASE": "abc123", "CHANGED_TESTS_BASE": "", "CEILING_BASE": "abc123", "NZELA_WARM_SLOTS": "1"})
                target = os.path.join(temp, area)
                os.makedirs(target, exist_ok=True)
                mounts = {target: "/target"}
                if current.warm_volume:
                    warm = os.path.join(target, "warm")
                    os.makedirs(warm, exist_ok=True)
                    mounts[warm] = "/warm"
                calls.clear()
                caps.clear()
                self.assertEqual(areas.run_in_image(current, area, temp, mounts,
                                                     target, io.BytesIO(), cpus=tuple(range(current.cpus)), run=run), 0)
                self.assertEqual(caps, [current.cpus, *([current.cpus] if current.service else [])])
                launched = [argv for argv in calls if argv[:2] == ["docker", "run"]]
                self.assertTrue(any(f"--cpus={current.cpus}" in argv for argv in launched))
                if current.service:
                    service = launched[0]
                    self.assertEqual(service.count(f"--cpus={current.cpus}"), 1)
                    self.assertEqual(option(service, "--cpuset-cpus"), ",".join(map(str, range(current.cpus))))
                    self.assertEqual(option(service, "--network-alias"), "postgres")
                    self.assertEqual(option(service, "--network"), area)
                if current.warm_volume:
                    argv = launched[-1]
                    env = environment(argv)
                    self.assertEqual(option(argv, "--cpus"), str(current.cpus))
                    self.assertEqual(option(argv, "--cpuset-cpus"), ",".join(map(str, range(current.cpus))))
                    self.assertEqual((env["CARGO_TARGET_DIR"], env["CARGO_BUILD_JOBS"], env["RUST_TEST_THREADS"]),
                                     (f"/target/{area}", str(current.cpus), str(current.cpus)))
                    self.assertEqual(env["CARGO_INCREMENTAL"], "1")
                    self.assertEqual(argv.count(f"{warm}:/warm"), 1)
                    if area in ("rust", "compat", "e2e"):
                        self.assertIn("SCCACHE_DIR=/warm/sccache", argv)
                    self.assertNotIn("${{", " ".join(argv))
                    self.assertIn("/warm", shell_script(argv))

    def test_native_android_launch_has_a_kernel_quota_and_matching_view(self):
        import yaml
        with open(CI) as source:
            current = plan.job_for(yaml.safe_load(source), "android", {})
        class Meter:
            seconds = 0.0
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
        captured = {}
        def start(argv, **kwargs):
            captured.update(argv=argv, env=kwargs["env"])
            return SimpleNamespace(pid=17)
        usage = SimpleNamespace(ru_utime=0.0, ru_stime=0.0)
        with patch.object(areas, "CpuMeter", return_value=Meter()), \
             patch.object(areas, "user_scopes", return_value=True), \
             patch.object(areas.subprocess, "Popen", side_effect=start), \
             patch.object(areas.os, "wait4", return_value=(17, 0, usage)):
            status, _ = areas.run_on_host("android", "/work", "/target", ["gradle-slot"], io.BytesIO(), job=current)
        self.assertEqual(status, 0)
        self.assertIn("AllowedCPUs=36,37", captured["argv"])
        self.assertIn("taskset", captured["argv"])
        self.assertEqual(captured["env"]["JAVA_TOOL_OPTIONS"], "-XX:ActiveProcessorCount=2")
        self.assertTrue(captured["env"]["GRADLE_OPTS"].startswith("-Dorg.gradle.workers.max=2 "))

    def android_env(self):
        class Meter:
            seconds = 0.0
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return None
        import yaml
        with open(CI) as source:
            current = plan.job_for(yaml.safe_load(source), "android", {})
        captured = {}
        def start(argv, **kwargs):
            captured["env"] = kwargs["env"]
            captured["registry"] = [o for o in kwargs["env"]["GRADLE_OPTS"].split() if o.startswith("-Dorg.gradle.daemon.registry.base=")]
            return SimpleNamespace(pid=17)
        usage = SimpleNamespace(ru_utime=0.0, ru_stime=0.0)
        with patch.object(areas, "CpuMeter", return_value=Meter()), \
             patch.object(areas, "user_scopes", return_value=True), \
             patch.object(areas.subprocess, "Popen", side_effect=start), \
             patch.object(areas.os, "wait4", return_value=(17, 0, usage)):
            areas.run_on_host("android", "/work", "/target", ["gradle-slot"], io.BytesIO(), job=current)
        return captured["registry"]

    def test_the_android_area_keeps_gradle_daemons_in_a_registry_of_its_own_run(self):
        registry = self.android_env()
        self.assertEqual(len(registry), 1)
        path = registry[0].split("=", 1)[1]
        self.assertTrue(os.path.basename(os.path.dirname(path)).startswith("nzela-gh-"), path)

    def test_two_android_runs_never_share_a_daemon_registry(self):
        self.assertNotEqual(self.android_env(), self.android_env())

    def tree(self, wrapper=True):
        root = tempfile.mkdtemp()
        if wrapper:
            os.makedirs(os.path.join(root, "scripts"))
            open(os.path.join(root, "scripts", "rustc-wrapper.sh"), "w").close()
        return root

    def test_sccache_is_used_through_the_trees_wrapper(self):
        root = self.tree()
        env = areas.cache_env(root, {"/h/sccache": "/sccache"})
        self.assertEqual(env["RUSTC_WRAPPER"], os.path.join(root, "scripts", "rustc-wrapper.sh"))
        self.assertEqual(env["SCCACHE_DIR"], "/sccache")
        self.assertEqual(env["SCCACHE_BASEDIRS"], root)

    def test_a_tree_without_the_wrapper_compiles_as_it_always_did(self):
        env = areas.cache_env(self.tree(wrapper=False), {"/h/sccache": "/sccache"})
        self.assertNotIn("RUSTC_WRAPPER", env)

    def test_no_sccache_mounted_means_no_wrapper(self):
        self.assertNotIn("RUSTC_WRAPPER", areas.cache_env(self.tree(), {}))

    def test_npm_reads_its_mounted_cache_and_prefers_it_to_the_network(self):
        env = areas.cache_env(self.tree(), {"/h/npm": "/npm"})
        self.assertEqual((env["npm_config_cache"], env["npm_config_prefer_offline"]), ("/npm", "true"))

    def test_container_jobs_use_one_target_path_inside_separate_host_mounts(self):
        from nzfleet import plan

        calls = []

        def run(argv, **_):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(areas.os, "getuid", return_value=1201), \
             patch.object(areas.os, "getgid", return_value=2302), \
             patch.object(areas.docker_budget, "container_flags", return_value=["--cpus=4"]):
            for index, slot in enumerate(("slot-a", "slot-b")):
                with tempfile.TemporaryDirectory() as root:
                    host_mount = os.path.join(root, slot)
                    calls.clear()
                    status = areas.run_in_image(plan.Job("rust", "rust:1.98-trixie"), f"rust-job-{index}", "/work",
                                                {host_mount: "/target"}, host_mount, io.BytesIO(), run=run)
                self.assertEqual(status, 0)
                job_argv = next(argv for argv in calls if argv[:2] == ["docker", "run"])
                self.assertIn(f"{host_mount}:/target", job_argv)
                self.assertIn("CARGO_TARGET_DIR=/target/rust", job_argv)
                driver = shell_script(job_argv)
                self.assertIn("chown -R 1201:2302 /work /target/rust", driver)

    def test_host_jobs_keep_their_physical_target_directory(self):
        class Meter:
            seconds = 0.0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        root = self.tree()
        process = SimpleNamespace(pid=17)
        usage = SimpleNamespace(ru_utime=1.0, ru_stime=0.5)
        captured = {}

        def start(argv, **kwargs):
            env = kwargs["env"]
            captured.update(argv=argv, env=env,
                            runner_files=all(os.path.isfile(env[f"GITHUB_{channel}"])
                                             for channel in ("PATH", "ENV", "OUTPUT", "STEP_SUMMARY")))
            return process

        with patch.object(areas, "CpuMeter", return_value=Meter()), \
             patch.object(areas, "user_scopes", return_value=""), \
             patch.object(areas.subprocess, "Popen", side_effect=start), \
             patch.object(areas.os, "wait4", return_value=(17, 0, usage)):
            status, cpu = areas.run_on_host("secrets", root, "/warm/slot-a", [], io.BytesIO())

        self.assertEqual((status, cpu), (0, 1.5))
        self.assertEqual(captured["env"]["CARGO_TARGET_DIR"], "/warm/slot-a/secrets")
        self.assertEqual((captured["env"]["CI"], captured["env"]["GITHUB_ACTIONS"]), ("true", "true"))
        self.assertTrue(captured["runner_files"])

    def test_console_install_has_private_modules_and_deploy_installs_none(self):
        import yaml

        with open(CI) as source:
            workflow = yaml.safe_load(source)
        jobs = {area: plan.job_for(workflow, area, {"NZELA_BASE": "abc123"}) for area in ("console", "deploy")}
        with tempfile.TemporaryDirectory(prefix="node mounts 'literal;$() ") as temp:
            root, target = os.path.join(temp, "tree"), os.path.join(temp, "warm target")
            admin = os.path.join(root, "frontend", "admin")
            os.makedirs(os.path.join(admin, "node_modules"))
            mounts = {target: "/target", os.path.join(temp, "warm"): "/warm"}
            commands = {}

            def capture(argv, **_):
                if argv[:2] == ["docker", "run"]:
                    commands[argv[argv.index("--name") + 1]] = argv
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(areas.docker_budget, "container_flags", return_value=[]):
                for area, job in jobs.items():
                    self.assertEqual(areas.run_in_image(job, area, root, mounts, target, io.BytesIO(), run=capture), 0)

            console_mount = f"{os.path.join(target, 'console', 'node_modules')}:{os.path.join(admin, 'node_modules')}"
            self.assertIn(console_mount, commands["console"])
            self.assertNotIn(console_mount, commands["deploy"])
            self.assertIn("npm ci --prefer-offline", jobs["console"].steps)
            self.assertFalse(any("npm ci" in step for step in jobs["deploy"].steps))


class TheCacheCountsAJobPrints(unittest.TestCase):
    def test_the_last_line_is_the_one_read(self):
        text = "sccache: 3 hits, 4 misses\nrust\nsccache: 10 hits, 2 misses\n"
        self.assertEqual(areas.sccache_counts(text), (10, 2))

    def test_a_job_that_compiled_nothing_through_it_has_none(self):
        self.assertEqual(areas.sccache_counts("Finished `test` profile\n"), (0, 0))

    def test_a_line_that_merely_mentions_it_is_not_a_count(self):
        self.assertEqual(areas.sccache_counts("note: sccache: 3 hits, 4 misses are shown above\n"), (0, 0))


class StatsLines(unittest.TestCase):
    def test_a_job_with_a_cache_reports_its_hits(self):
        stat = {"wall_s": 90, "cpu_s": 150.0, "sccache_hits": 8, "sccache_misses": 1, "tests_passed": 0}
        self.assertEqual(areas.stats_line("rust", stat), "stats: rust 90s wall, 2.5 cpu-min, sccache 8 hits 1 misses")

    def test_a_job_without_one_does_not_mention_it(self):
        stat = {"wall_s": 5, "cpu_s": 6.0, "sccache_hits": 0, "sccache_misses": 0, "tests_passed": 0}
        self.assertEqual(areas.stats_line("site", stat), "stats: site 5s wall, 0.1 cpu-min")

    def test_the_tests_that_passed_are_counted(self):
        stat = {"wall_s": 5, "cpu_s": 6.0, "sccache_hits": 0, "sccache_misses": 0, "tests_passed": 1387}
        self.assertEqual(areas.stats_line("rust", stat), "stats: rust 5s wall, 0.1 cpu-min, 1387 tests passed")


class TheTestsAnAreaSaysPassed(unittest.TestCase):
    def test_cargo_s_results_are_added_over_every_binary(self):
        text = "test result: ok. 1387 passed; 0 failed\nrunning\ntest result: ok. 3 passed; 0 failed\n"
        self.assertEqual(areas.tests_passed(text), 1390)

    def test_a_failed_binary_still_counts_the_tests_in_it_that_passed(self):
        self.assertEqual(areas.tests_passed("test result: FAILED. 12 passed; 1 failed\n"), 12)

    def test_the_android_line_counts_only_a_run_with_no_failure(self):
        self.assertEqual(areas.tests_passed("android tests: 1056 ran, 0 failed\n"), 1056)
        self.assertEqual(areas.tests_passed("android tests: 1056 ran, 2 failed\n"), 0)

    def test_playwright_s_summary_is_counted(self):
        self.assertEqual(areas.tests_passed("  2 skipped\n  391 passed (3.2m)\n"), 391)

    def test_an_area_that_says_nothing_has_none(self):
        self.assertEqual(areas.tests_passed("secrets: nothing in the tree is a credential\n"), 0)


class TheCpuAContainerUsed(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.scope = os.path.join(self.root, "docker-abc.scope")
        os.makedirs(self.scope)

    def write(self, usec):
        with open(os.path.join(self.scope, "cpu.stat"), "w") as f:
            f.write(f"usage_usec {usec}\nuser_usec 1\n")

    def docker(self, known):
        def run(argv, **_):
            name = argv[-1]
            return subprocess.CompletedProcess(argv, 0 if name in known else 1, "abc\n" if name in known else "", "")
        return run

    def test_the_last_reading_before_the_container_goes_is_what_it_used(self):
        self.write(2_500_000)
        with areas.CpuMeter(["job"], self.docker({"job"}), self.root, every=0.01) as meter:
            while "job" not in meter.usec:
                pass
            self.write(7_000_000)
            while meter.usec["job"] != 7_000_000:
                pass
            os.remove(os.path.join(self.scope, "cpu.stat"))
        self.assertEqual(meter.seconds, 7.0)

    def test_it_asks_for_a_container_because_the_jobs_network_has_the_same_name(self):
        asked = []

        def run(argv, **_):
            asked.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "")
        with areas.CpuMeter(["job"], run, self.root, every=0.01):
            while not asked:
                pass
        self.assertIn("--type", asked[0])
        self.assertEqual(asked[0][asked[0].index("--type") + 1], "container")

    def test_a_container_that_never_started_used_nothing(self):
        with areas.CpuMeter(["job"], self.docker(set()), self.root, every=0.01) as meter:
            pass
        self.assertEqual(meter.seconds, 0.0)

    def test_the_job_and_its_service_are_added(self):
        os.makedirs(os.path.join(self.root, "docker-def.scope"))
        self.write(1_000_000)
        with open(os.path.join(self.root, "docker-def.scope", "cpu.stat"), "w") as f:
            f.write("usage_usec 3000000\n")

        def run(argv, **_):
            return subprocess.CompletedProcess(argv, 0, "abc\n" if argv[-1] == "job" else "def\n", "")
        with areas.CpuMeter(["job", "job-service"], run, self.root, every=0.01) as meter:
            while len(meter.usec) < 2:
                pass
        self.assertEqual(meter.seconds, 4.0)


class AnAreaRunOnTheHost(unittest.TestCase):
    def test_its_exit_code_and_the_cpu_of_what_it_ran_come_back(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "scripts"))
        script = os.path.join(root, "scripts", "checks.sh")
        with open(script, "w") as f:
            f.write(f'#!{sys.executable}\nimport sys, time\nend = time.time() + 2.5\nwhile time.time() < end: pass\nsys.exit(3)\n')
        os.chmod(script, 0o755)
        class NoContainerCpuMeter:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with tempfile.TemporaryFile() as out, mock.patch.object(areas, "CpuMeter", NoContainerCpuMeter), \
                mock.patch.object(areas, "user_scopes", return_value=""):
            rc, cpu = areas.run_on_host("secrets", root, root, [], out, cpus=tuple(sorted(tests.REAL_AFFINITY(0))[:2]))
        self.assertEqual(rc, 3)
        self.assertGreater(cpu, 0.0)



class TheWarmBuildDirectoryKeepsWhatWasUsedLast(unittest.TestCase):
    def _area(self, target, name, megabytes, age):
        path = os.path.join(target, name)
        os.makedirs(path)
        with open(os.path.join(path, "blob"), "wb") as f:
            f.truncate(megabytes * 2**20)
        os.utime(path, (age, age))

    def test_only_the_oldest_area_goes_when_the_directory_is_over_its_cap(self):
        from nzfleet import warm
        with tempfile.TemporaryDirectory() as target:
            self._area(target, "rust", 3 * 1024, 300)
            self._area(target, "e2e", 3 * 1024, 100)
            self._area(target, "console", 3 * 1024, 200)
            self.assertEqual(warm.shrink(target, 6), ["e2e"])
            self.assertEqual(sorted(os.listdir(target)), ["console", "rust"])

    def test_a_directory_within_its_cap_is_left_whole(self):
        from nzfleet import warm
        with tempfile.TemporaryDirectory() as target:
            self._area(target, "rust", 1024, 100)
            self.assertEqual(warm.shrink(target, 6), [])
            self.assertEqual(os.listdir(target), ["rust"])


class AnAreaRunOnTheHostIsRecentlyUsed(unittest.TestCase):
    def test_its_build_directory_is_touched_so_the_oldest_area_is_the_one_unused(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "scripts"))
        script = os.path.join(root, "scripts", "checks.sh")
        with open(script, "w") as f:
            f.write(f"#!{sys.executable}\n")
        os.chmod(script, 0o755)
        os.makedirs(os.path.join(root, "secrets"))
        os.utime(os.path.join(root, "secrets"), (100, 100))
        with tempfile.TemporaryFile() as out, mock.patch.object(areas, "user_scopes", return_value=""):
            areas.run_on_host("secrets", root, root, [], out, cpus=tuple(sorted(tests.REAL_AFFINITY(0))[:2]))
        self.assertGreater(os.stat(os.path.join(root, "secrets")).st_mtime, 100)


class TheAndroidSlots(unittest.TestCase):
    SCRIPT = os.path.join(os.path.dirname(__file__), "..", "bin", "gradle-slot")
    PROBE = ["bash", "-c", "readlink /proc/$$/fd/9"]

    def _slot_taken(self, locks, held, batch):
        import fcntl
        files = []
        for i in held:
            f = open(os.path.join(locks, f"nzpm-gradle.slot{i}"), "w")
            fcntl.flock(f, fcntl.LOCK_EX)
            files.append(f)
        env = {**os.environ, "NZPM_GRADLE_LOCKS": locks}
        env.pop("NZFLEET_BATCH", None)
        if batch:
            env["NZFLEET_BATCH"] = "1"
        try:
            return os.path.basename(subprocess.run([self.SCRIPT, *self.PROBE], env=env, capture_output=True, text=True, check=True).stdout.strip())
        finally:
            for f in files:
                f.close()

    def test_a_batch_takes_the_last_slot_when_the_others_are_busy(self):
        with tempfile.TemporaryDirectory() as locks:
            self.assertEqual(self._slot_taken(locks, [1, 2], batch=True), "nzpm-gradle.slot3")

    def test_an_agent_takes_the_second_slot_rather_than_the_batch_one(self):
        with tempfile.TemporaryDirectory() as locks:
            self.assertEqual(self._slot_taken(locks, [1], batch=False), "nzpm-gradle.slot2")


if __name__ == "__main__":
    unittest.main()
