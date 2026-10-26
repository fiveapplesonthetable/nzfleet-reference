import glob
import os
import re
import shlex
import signal
import subprocess
import tempfile
import unittest

import yaml

from nzfleet import plan
from tests.argv import environment, option, shell_script

CI = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".github", "workflows", "ci.yaml")
WORKFLOW = yaml.safe_load(open(CI))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
EXPRESSIONS = {"NZELA_BASE": "abc123", "CHANGED_TESTS_BASE": "", "CEILING_BASE": "abc123", "NZELA_WARM_SLOTS": "1"}

NODE = "node:22-trixie"
RUST = "rust:1.98-trixie"
PLAYWRIGHT = "mcr.microsoft.com/playwright"


def job(area):
    return plan.job_for(WORKFLOW, area, EXPRESSIONS)


def checks_script_path(job):
    """The checks.sh the job's last step runs, resolved from the step's working directory."""
    command = shlex.split(job.steps[-1])
    script = next(word for word in command if word.endswith("scripts/checks.sh"))
    return os.path.normpath(os.path.join(ROOT, job.workdir or "", script)), command[command.index(script) + 1:]


def assert_checks_run_from_workdir(test, current, area):
    script, args = checks_script_path(current)
    test.assertEqual(script, os.path.join(ROOT, "scripts", "checks.sh"))
    test.assertEqual(args, [area])


def checks_run(job):
    """The areas the job's last step hands to checks.sh, run with the environment the batch gives it."""
    script = job.steps[-1].replace("scripts/checks.sh", "echo")
    env = {"PATH": os.environ["PATH"], **job.step_envs[-1]}
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout.strip()


class WhereCiRunsEachArea(unittest.TestCase):
    def test_console_jobs_share_one_build_and_the_crawl_runs_after_merge(self):
        jobs = WORKFLOW["jobs"]
        self.assertNotIn("console-crawl", jobs)
        self.assertEqual(jobs["console-browser"]["needs"], ["select", "console"])
        self.assertNotIn("console-capture", jobs)
        self.assertEqual(
            [step.get("with", {}).get("name") for step in jobs["console"]["steps"] if "upload-artifact" in step.get("uses", "")],
            ["console-dist"],
        )
        self.assertIn("console-dist", [step.get("with", {}).get("name") for step in jobs["console-browser"]["steps"]])
        with open(os.path.join(ROOT, ".github", "workflows", "console-crawl.yaml")) as source:
            postmerge = yaml.safe_load(source)
        self.assertEqual(postmerge["jobs"]["crawl"]["strategy"]["matrix"]["shard"], [1, 2, 3, 4])
        self.assertEqual(postmerge[True]["workflow_run"]["workflows"], ["Test environment deployment"])
        self.assertEqual(postmerge[True]["workflow_run"]["branches"], ["main"])
        with open(os.path.join(ROOT, ".github", "workflows", "console-capture.yaml")) as source:
            capture = yaml.safe_load(source)
        self.assertEqual(capture[True]["workflow_run"]["workflows"], ["Test environment deployment"])
        self.assertEqual(capture[True]["workflow_run"]["branches"], ["main"])
        self.assertNotIn("workflow_dispatch", capture[True])

    def test_postsubmit_cannot_start_before_the_deploy_has_finished(self):
        def workflow(name):
            with open(os.path.join(ROOT, ".github", "workflows", name)) as source:
                return yaml.safe_load(source)

        post, deploy = workflow("postsubmit.yaml"), workflow("deploy.yaml")
        trigger = post[True]["workflow_run"]
        self.assertEqual(trigger["workflows"], [deploy["name"]])
        self.assertNotIn(WORKFLOW["name"], trigger["workflows"])
        self.assertEqual((trigger["types"], trigger["branches"]), (["completed"], ["main"]))
        self.assertEqual(deploy[True]["workflow_run"]["workflows"], [WORKFLOW["name"]])
        self.assertEqual(post["concurrency"], {"group": "postsubmit-main", "cancel-in-progress": True})
        self.assertNotIn("conclusion", post["jobs"]["decide"]["if"])
        self.assertIn("ci.yaml/runs?head_sha=", post["jobs"]["decide"]["steps"][0]["run"])

    def test_fleet_checks_run_separately_from_deploy(self):
        self.assertEqual(job("fleet").steps[-1], "scripts/checks.sh fleet")
        with open(os.path.join(ROOT, "scripts", "areas", "deploy.sh")) as source:
            deploy = source.read().split("check_fleet()", 1)[0]
        self.assertNotIn("unittest discover -q tests", deploy)

    def test_rust_checks_run_the_tests_with_a_pinned_nextest_binary(self):
        check_steps = [step.get("run", "") for step in WORKFLOW["jobs"]["check"]["steps"]]
        self.assertFalse(any("cargo install cargo-nextest" in step for step in check_steps))
        self.assertTrue(any("tools/nzfleet/runner/install-nextest.sh" in step for step in check_steps))
        self.assertNotIn("rust_tests", WORKFLOW["jobs"])
        self.assertFalse(any(step.get("uses", "").startswith("actions/upload-artifact") for step in WORKFLOW["jobs"]["check"]["steps"]))

    def test_every_area_ci_runs_in_an_image_is_run_in_that_image(self):
        expected = {"rust": RUST, "compat": RUST, "fuzz": RUST, "e2e": RUST, "secrets": NODE, "site": NODE,
                    "deploy": NODE, "fleet": NODE, "console": PLAYWRIGHT}
        for area, image in expected.items():
            self.assertTrue(job(area).image.startswith(image), area)

    def test_the_images_are_the_ones_ci_pins_by_digest(self):
        for area in ("rust", "deploy", "console", "fleet"):
            self.assertIn("@sha256:", job(area).image)

    def test_the_areas_ci_runs_on_the_host_have_no_image(self):
        for area in ("android", "image", "formal"):
            self.assertEqual(job(area).image, "", area)

    def test_an_area_with_no_job_in_ci_runs_on_the_host(self):
        self.assertEqual(job("coverage"), plan.Job("coverage"))

    def test_docker_is_only_where_ci_has_it(self):
        for area in ("rust", "compat", "fuzz", "e2e", "secrets", "site", "deploy", "fleet", "console"):
            argv = plan.job_argv(job(area), "n", "net", {}, "/w", "1:1", {})
            self.assertNotIn("/var/run/docker.sock", " ".join(argv), area)

    def test_console_batch_runs_the_combined_area_in_the_browser_image(self):
        console = job("console")
        self.assertEqual(console.cpus, 12)
        self.assertIn("npm ci --prefer-offline", console.steps)
        assert_checks_run_from_workdir(self, console, "console")


class TheBaseIsFetchedShallow(unittest.TestCase):
    """Every job but `select` holds the base commit and nothing behind it, so nothing it runs may need a merge base."""

    HISTORY = re.compile(r"git\b.*\b(diff|log|rev-list|merge-base)\b")
    THREE_DOT = re.compile(r'[\w"}\)]\.\.\.[\w"$\{]')
    # Reads NZELA_BASE only when CEILING_BASE is unset, and takes CEILING_BASE itself as the base otherwise.
    GUARDED = {"crate-ceilings.sh"}

    def fetches(self):
        for name, spec in WORKFLOW["jobs"].items():
            for step in spec.get("steps", []):
                if step.get("name") == plan.FETCH_BASE_STEP:
                    yield name, step["run"]

    def test_every_job_fetches_the_base_at_depth_one(self):
        found = list(self.fetches())
        self.assertGreater(len(found), 10)
        for name, run in found:
            self.assertEqual(run, 'git fetch --no-tags --depth=1 origin "$NZELA_BASE"', name)

    def test_no_check_outside_select_diffs_against_the_base_with_three_dots(self):
        sources = [os.path.join(ROOT, "scripts", "*.sh"), os.path.join(ROOT, "scripts", "*.py"),
                   os.path.join(ROOT, "scripts", "areas", "*.sh")]
        offenders = []
        for path in sorted(p for pattern in sources for p in glob.glob(pattern)):
            if os.path.basename(path) in self.GUARDED or path.endswith("-test.sh"):
                continue
            with open(path) as source:
                for number, line in enumerate(source, 1):
                    if line.lstrip().startswith("#") or "--is-ancestor" in line:
                        continue
                    if self.HISTORY.search(line) and (self.THREE_DOT.search(line) or "merge-base" in line):
                        offenders.append(f"{os.path.relpath(path, ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_detector_sees_a_three_dot_diff(self):
        line = 'git diff --name-only "$NZELA_BASE"...HEAD'
        self.assertTrue(self.HISTORY.search(line) and self.THREE_DOT.search(line))

    def test_a_two_tree_diff_against_a_depth_one_pair_needs_no_merge_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, job = os.path.join(tmp, "origin"), os.path.join(tmp, "job")
            env = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

            def git(where, *args):
                return subprocess.run(["git", "-C", where, *args], env=env, capture_output=True, text=True)

            subprocess.run(["git", "init", "-q", "-b", "main", origin], env=env, check=True)
            for n in ("one", "two", "three"):
                with open(os.path.join(origin, n), "w") as f:
                    f.write(n)
                git(origin, "add", "-A")
                git(origin, "commit", "-qm", n)
            base, head = git(origin, "rev-parse", "HEAD~2").stdout.strip(), git(origin, "rev-parse", "HEAD").stdout.strip()
            subprocess.run(["git", "init", "-q", job], env=env, check=True)
            for sha in (head, base):
                git(job, "fetch", "-q", "--no-tags", "--depth=1", "file://" + origin, sha)
            git(job, "checkout", "-q", "--detach", head)
            self.assertEqual(git(job, "diff", "--name-only", base, "HEAD").stdout.split(), ["three", "two"])
            self.assertNotEqual(git(job, "diff", "--name-only", f"{base}...HEAD").returncode, 0)


class WhatCiSetsUp(unittest.TestCase):
    def test_the_rust_areas_get_their_own_postgres_by_the_name_ci_gives_it(self):
        for area in ("rust", "compat", "fuzz", "e2e"):
            self.assertEqual(job(area).service["name"], "postgres", area)
            self.assertEqual(job(area).env["DATABASE_URL"], "postgres://nzela:nzela@postgres:5432/nzela", area)

    def test_rust_area_postgres_argv_has_the_bounded_connection_budget(self):
        for area in ("rust", "compat", "fuzz", "e2e"):
            argv = plan.service_argv(job(area), "svc", "net")
            self.assertIn(("-c", "max_connections=300"), list(zip(argv, argv[1:])), area)

    def test_compose_test_postgres_command_has_the_bounded_connection_budget(self):
        with open(os.path.join(ROOT, "deploy", "compose.test.yaml")) as source:
            compose = yaml.safe_load(source)
        command = shlex.split(compose["services"]["postgres"]["command"])
        self.assertIn(("-c", "max_connections=300"), list(zip(command, command[1:])))

    def test_pre_push_postgres_command_has_the_bounded_connection_budget(self):
        with open(os.path.join(ROOT, "tools", "nzfleet", "bin", "pre-push-db.sh")) as source:
            script = source.read()
        command = re.search(r"(?ms)^docker run -d .*?^\s+postgres:16 (.*?) >/dev/null$", script)
        self.assertIsNotNone(command)
        argv = shlex.split(command.group(1).replace("\\\n", " "))
        self.assertIn(("-c", "max_connections=300"), list(zip(argv, argv[1:])))

    def test_fuzz_compiles_in_the_rust_jobs_environment_and_runs_its_own_check(self):
        self.assertEqual(checks_run(job("fuzz")), "fuzz")
        self.assertEqual(job("fuzz").image, job("rust").image)

    def test_rust_runs_only_its_own_check_in_the_job_it_shares_with_fuzz(self):
        self.assertEqual(checks_run(job("rust")), "rust")

    def test_areas_without_a_service_start_none(self):
        for area in ("secrets", "site", "deploy", "console"):
            self.assertIsNone(job(area).service, area)

    def test_the_steps_ci_runs_before_the_checks_run_before_them(self):
        steps = job("deploy").steps
        self.assertIn("ci-tools.sh shell-checks", steps[0])
        self.assertEqual(steps[-1], "scripts/checks.sh deploy")

    def test_deploy_runs_without_console_dependencies(self):
        argv = plan.job_argv(job("deploy"), "n", "net", {}, "/w", "1:1", {})
        commands = argv[argv.index("nzfleet") + 1:]
        self.assertEqual(commands[-1], "scripts/checks.sh deploy")
        self.assertFalse(any("npm ci" in command for command in commands))

    def test_the_migrations_are_applied_by_the_step_ci_applies_them_in(self):
        self.assertTrue(any("psql" in s for s in job("rust").steps))

    def test_the_console_runs_where_ci_runs_it(self):
        self.assertEqual(job("console").workdir, "frontend/admin")
        assert_checks_run_from_workdir(self, job("console"), "console")

    def test_contract_finds_the_generator_without_an_action_path_file(self):
        self.assertEqual(job("contract").steps[-1],
                         'flock /warm/typeshare.lock sh -c \'PATH="/warm/typeshare/bin:$PATH" scripts/checks.sh contract\'')

    def test_an_expression_in_ci_takes_the_value_the_batch_gives_it(self):
        self.assertEqual(job("compat").env["NZELA_BASE"], "abc123")

    def test_an_expression_with_no_value_is_refused(self):
        with self.assertRaisesRegex(ValueError, "NZELA_BASE"):
            plan.job_for(WORKFLOW, "compat", {})

    def test_every_action_ci_uses_in_an_image_has_a_stand_in(self):
        for area in ("rust", "compat", "fuzz", "e2e", "secrets", "site", "deploy", "console"):
            job(area)

    def test_an_action_without_a_stand_in_is_refused(self):
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"}, "steps": [{"uses": "acme/setup@v1"}]}}}
        with self.assertRaisesRegex(ValueError, "acme/setup"):
            plan.job_for(workflow, "site", {})

    def test_a_conditional_step_is_refused(self):
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"}, "steps": [{"run": "true", "if": "always()"}]}}}
        with self.assertRaisesRegex(ValueError, "if"):
            plan.job_for(workflow, "site", {})

    def test_a_step_working_directory_runs_only_that_step_there(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = os.path.join(temp, "workspace")
            target = "admin space;$(false)'literal"
            os.makedirs(os.path.join(worktree, target))
            with open(os.path.join(worktree, target, "marker"), "w") as marker:
                marker.write("ready")
            workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"}, "steps": [
                {"run": "test -f marker && printf 'FLEET_DIRECTORY=ready\\n' >> \"$GITHUB_ENV\"",
                 "working-directory": target},
                {"run": "test ! -e marker && test \"$PWD\" = \"$WORKSPACE\" && "
                        "test \"$FLEET_DIRECTORY\" = ready"},
            ]}}}
            argv = plan.job_argv(plan.job_for(workflow, "site", {}), "n", "net", {}, worktree,
                                 f"{os.getuid()}:{os.getgid()}", {})
            driver = shell_script(argv)
            result = subprocess.run(["bash", "-c", driver, "nzfleet", *argv[argv.index("nzfleet") + 1:]],
                                    cwd=worktree, env={**os.environ, "WORKSPACE": worktree}, check=False)
            self.assertEqual(result.returncode, 0)

    def test_a_step_directory_overrides_the_job_default_from_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = os.path.join(temp, "workspace")
            os.makedirs(os.path.join(worktree, "job"))
            os.makedirs(os.path.join(worktree, "other"))
            workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"},
                "defaults": {"run": {"working-directory": "job"}}, "steps": [
                    {"run": "test \"$PWD\" = \"$JOB_CWD\""},
                    {"run": "test \"$PWD\" = \"$STEP_CWD\"", "working-directory": "other"},
                    {"run": "test \"$PWD\" = \"$JOB_CWD\""},
                ]}}}
            argv = plan.job_argv(plan.job_for(workflow, "site", {}), "n", "net", {}, worktree,
                                 f"{os.getuid()}:{os.getgid()}", {})
            driver = shell_script(argv)
            result = subprocess.run(["bash", "-c", driver, "nzfleet", *argv[argv.index("nzfleet") + 1:]],
                                    cwd=os.path.join(worktree, "job"), env={**os.environ,
                                    "JOB_CWD": os.path.join(worktree, "job"),
                                    "STEP_CWD": os.path.join(worktree, "other")}, check=False)
            self.assertEqual(result.returncode, 0)

    def test_a_step_directory_expression_is_refused(self):
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"}, "steps": [
            {"run": "true", "working-directory": "${{ matrix.path }}"}]}}}
        with self.assertRaisesRegex(ValueError, "working-directory"):
            plan.job_for(workflow, "site", {})


class TheCommands(unittest.TestCase):
    def test_batch_checkout_has_the_merged_head_and_only_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            source = os.path.join(temp, "source")
            destination = os.path.join(temp, "checkout")
            os.makedirs(source)
            subprocess.run(["git", "init", "-q", source], check=True)
            for contents in ("main", "merged"):
                if contents == "merged":
                    subprocess.run(["git", "-C", source, "checkout", "-q", "--detach", "HEAD"], check=True)
                with open(os.path.join(source, "state"), "w") as file:
                    file.write(contents)
                subprocess.run(["git", "-C", source, "add", "state"], check=True)
                subprocess.run(["git", "-C", source, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                                "commit", "-qm", contents], check=True)
            script = os.path.join(ROOT, "tools", "nzfleet", "bin", "shallow-checkout.sh")
            result = subprocess.run(["bash", script, source, destination, "https://github.com/nzela-org/nzela.git"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(destination, "state")) as file:
                self.assertEqual(file.read(), "merged")
            self.assertEqual(subprocess.check_output(["git", "-C", destination, "rev-list", "--count", "HEAD"],
                                                     text=True).strip(), "1")
            self.assertEqual(subprocess.check_output(["git", "-C", destination, "remote"], text=True).split(), ["origin"])
            self.assertNotEqual(subprocess.run(["git", "-C", destination, "rev-parse", "upstream/main"],
                                               capture_output=True).returncode, 0)

    def test_runner_files_apply_path_and_environment_between_steps(self):
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"},
            "steps": [{"run": "printf '%s\\n' 'PATH=/x' >> \"$GITHUB_PATH\"; "
                              "printf '%s\\n' 'FLEET_VALUE=ready' 'MULTI<<EOF' 'first' 'second' 'EOF' >> \"$GITHUB_ENV\""},
                      {"run": "test \"$GITHUB_ACTIONS\" = true && test \"$CI\" = true && "
                              "test \"$GITHUB_REF\" = refs/heads/main && test \"$FLEET_VALUE\" = ready && "
                              "test \"${PATH%%:*}\" = 'PATH=/x' && "
                              "test \"$MULTI\" = \"$(printf 'first\\nsecond')\" && "
                              "test -f \"$GITHUB_OUTPUT\" && test -f \"$GITHUB_STEP_SUMMARY\""}]}}}
        with tempfile.TemporaryDirectory() as root:
            argv = plan.job_argv(plan.job_for(workflow, "site", {}), "n", "net", {}, root,
                                 f"{os.getuid()}:{os.getgid()}", {})
            job_env = environment(argv)
            result = subprocess.run(["bash", "-c", shell_script(argv), "nzfleet",
                                     *argv[argv.index("nzfleet") + 1:]], cwd=root,
                                    env={**os.environ, **job_env}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runner_files_live_outside_the_workspace_every_area_shares(self):
        # Areas run at once over one checkout, and each one's chown -R walks it.
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"},
            "steps": [{"run": "case \"$GITHUB_ENV\" in \"$PWD\"/*) exit 1;; esac; test -z \"$(ls -A)\""}]}}}
        with tempfile.TemporaryDirectory() as root:
            argv = plan.job_argv(plan.job_for(workflow, "site", {}), "n", "net", {}, root,
                                 f"{os.getuid()}:{os.getgid()}", {})
            result = subprocess.run(["bash", "-c", shell_script(argv), "nzfleet",
                                     *argv[argv.index("nzfleet") + 1:]], cwd=root,
                                    env={**os.environ, **environment(argv)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def handback_with(self, chown_says: str) -> subprocess.CompletedProcess:
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.CI_RUNS_ON && '--cpus=1' || '' }}"},
            "steps": [{"run": "true"}]}}}
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as bin_dir:
            with open(os.path.join(bin_dir, "chown"), "w") as shim:
                shim.write(f"#!/bin/sh\necho \"chown: changing ownership of '/sccache/x': {chown_says}\" >&2\nexit 1\n")
            os.chmod(os.path.join(bin_dir, "chown"), 0o755)
            argv = plan.job_argv(plan.job_for(workflow, "site", {}), "n", "net", {}, root,
                                 f"{os.getuid()}:{os.getgid()}", {})
            return subprocess.run(["bash", "-c", shell_script(argv), "nzfleet", *argv[argv.index("nzfleet") + 1:]],
                                  cwd=root, capture_output=True, text=True,
                                  env={**os.environ, **environment(argv), "PATH": f"{bin_dir}:{os.environ['PATH']}"})

    def test_a_file_another_area_removed_during_the_handback_does_not_fail_the_job(self):
        result = self.handback_with("No such file or directory")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_handback_the_host_refuses_still_fails_the_job(self):
        result = self.handback_with("Operation not permitted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Operation not permitted", result.stderr)

    def test_native_container_caps_match_ci_for_every_bounded_job(self):
        expected = {"rust": (7, 4), "compat": (3, 1), "e2e": (3, 1), "console": (12, 0), "fleet": (1, 0),
                    "contract": (1, 0), "secrets": (1, 0), "site": (1, 0), "deploy": (1, 0)}
        for area, caps in expected.items():
            with self.subTest(area=area):
                self.assertEqual((job(area).cpus, job(area).service_cpus), caps)
        self.assertNotIn("NZELA_CONSOLE_WORKERS", job("console").env)

    def test_unsupported_cpu_expression_is_refused(self):
        workflow = {"jobs": {"site": {"container": {"image": "x", "options": "${{ vars.OTHER && '--cpus=6' || '' }}"}}}}
        with self.assertRaisesRegex(ValueError, "CPU options"):
            plan.job_for(workflow, "site", {})

    def test_android_native_view_and_workers_are_five(self):
        self.assertEqual(job("android").env,
                         {"JAVA_TOOL_OPTIONS": "-XX:ActiveProcessorCount=5", "GRADLE_OPTS": "-Dorg.gradle.workers.max=5"})

    def test_android_job_without_either_resource_cap_is_refused(self):
        for missing in ("JAVA_TOOL_OPTIONS", "GRADLE_OPTS"):
            with self.subTest(missing=missing):
                workflow = {"jobs": {"android": {"env": {
                    key: value for key, value in WORKFLOW["jobs"]["android"]["env"].items() if key != missing
                }}}}
                with self.assertRaisesRegex(ValueError, "missing processor or Gradle worker cap"):
                    plan.job_for(workflow, "android", {})

    def test_a_job_container_reaps_its_children(self):
        self.assertIn("--init", plan.job_argv(job("rust"), "n", "net", {}, "/w", "1:1", {}))

    def test_the_service_starts_on_the_jobs_network_with_ci_options(self):
        for area in ("rust", "compat", "e2e"):
            service = plan.service_argv(job(area), "svc", "net")
            self.assertEqual(option(service, "--network"), "net", area)
            self.assertEqual(option(service, "--network-alias"), "postgres", area)
            self.assertEqual(environment(service)["POSTGRES_DB"], "nzela", area)
            self.assertIn("postgres:16-alpine", service, area)
            command = service[service.index("postgres:16-alpine") + 1:]
            self.assertIn("fsync=off", command, area)
            self.assertTrue(option(service, "--tmpfs").startswith("/var/lib/postgresql/data:rw"), area)
            self.assertEqual(any(flag.startswith("--memory=") for flag in service), area == "rust", area)

    def test_warm_jobs_keep_only_supported_mount_and_step_environment(self):
        for area in ("rust", "compat", "e2e", "contract"):
            current = job(area)
            self.assertTrue(current.warm_volume, area)
            self.assertEqual(current.step_envs[0], {"CI_WARM_OVERRIDE": "native-cache", "CI_SELF_HOSTED": "native"})
            argv = plan.job_argv(current, "n", "net", {"/owned/warm": "/warm"}, "/w", "1000:1000", {})
            self.assertEqual(argv.count("/owned/warm:/warm"), 1)
            self.assertIn("/warm", shell_script(argv))
            self.assertIn("export CI_WARM_OVERRIDE=native-cache\nexport CI_SELF_HOSTED=native\npython3 tools/nzfleet/runner/ci-warm-cache.py mounted", argv)
            self.assertNotIn("${{", " ".join(argv))

    def test_every_job_that_mounts_warm_reads_the_same_pull_request_setting(self):
        warm_jobs = {name: spec for name, spec in WORKFLOW["jobs"].items()
                     if any(str(v).endswith(":/warm") for v in (spec.get("container") or {}).get("volumes") or [])}
        self.assertGreaterEqual(len(warm_jobs), 4)
        volumes, overrides, slots = set(), set(), set()
        for name, spec in warm_jobs.items():
            volumes.update(spec["container"]["volumes"])
            overrides.update(str(s["env"]["CI_WARM_OVERRIDE"]) for s in spec["steps"] if s.get("name") == "Verify warm cache mount")
            if "NZELA_WARM_SLOTS" in (spec.get("env") or {}):
                slots.add(str(spec["env"]["NZELA_WARM_SLOTS"]))
        self.assertEqual(len(volumes), 1, volumes)
        self.assertEqual(len(overrides), 1, overrides)
        self.assertEqual(len(slots), 1, slots)
        self.assertIn("vars.CI_PR_WARM_DIR", next(iter(volumes)))
        self.assertIn("vars.CI_PR_WARM_DIR", next(iter(overrides)))
        for text in (next(iter(volumes)), next(iter(overrides))):
            self.assertIn("vars.CI_PR_WARM_DIR || format('/srv/fleet/nzela-pr/ci-cache/repo-{0}', github.repository_id)", text)
            self.assertNotIn("nzela-warm", text)
        self.assertIn("github.event_name == 'pull_request' && '5' || '1'", next(iter(slots)))

    def test_a_batch_runs_the_whole_rust_or_fuzz_area_not_one_lane_of_it(self):
        for area in ("rust", "fuzz"):
            current = job(area)
            self.assertEqual(current.steps[-1], f"scripts/checks.sh {area}")
            self.assertEqual(current.step_envs[-1], {})

    def test_unknown_warm_volume_or_step_expression_is_refused(self):
        import copy
        workflow = copy.deepcopy(WORKFLOW)
        workflow["jobs"]["check"]["container"]["volumes"] = ["${{ vars.OTHER }}:/warm"]
        with self.assertRaisesRegex(ValueError, "container volumes"):
            plan.job_for(workflow, "rust", EXPRESSIONS)
        workflow = copy.deepcopy(WORKFLOW)
        next(s for s in workflow["jobs"]["check"]["steps"] if s.get("name") == "Verify warm cache mount")["env"]["CI_SELF_HOSTED"] = "${{ vars.OTHER }}"
        with self.assertRaisesRegex(ValueError, "step environment"):
            plan.job_for(workflow, "rust", EXPRESSIONS)

    def test_the_job_runs_its_steps_in_order_as_root_in_its_working_directory(self):
        argv = plan.job_argv(job("console"), "n", "net", {}, "/w/x", "1000:1000", {})
        self.assertEqual(option(argv, "-w"), "/w/x/frontend/admin")
        self.assertEqual(option(argv, "--user"), "root")
        self.assertEqual(argv[argv.index("nzfleet") + 1:], list(job("console").steps))

    def test_the_worktree_is_mounted_where_it_is_on_the_host_and_the_repository_read_only(self):
        argv = plan.job_argv(job("rust"), "n", "net", {"/r/.git": "/r/.git:ro"}, "/w/x", "1:1", {})
        self.assertIn("/w/x:/w/x", argv)
        self.assertIn("/r/.git:/r/.git:ro", argv)

    def test_what_the_job_wrote_is_handed_back_but_the_read_only_repository_is_not_touched(self):
        argv = plan.job_argv(job("rust"), "n", "net", {"/t": "/target", "/r/.git": "/r/.git:ro"}, "/w/x", "7:8", {})
        driver = shell_script(argv)
        self.assertIn("chown -R 7:8 /w/x /target/rust ", driver)
        self.assertNotIn("/target ", driver)
        self.assertNotIn("/r/.git", driver)

    def test_a_read_only_target_is_excluded_from_ownership_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = os.path.join(temp, "worktree")
            handback = os.path.join(temp, "handback")
            executable = os.path.join(temp, "chown")
            os.makedirs(worktree)
            with open(executable, "w") as fake_chown:
                fake_chown.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HANDBACK\"\n")
            os.chmod(executable, 0o755)
            env = {**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"], "HANDBACK": handback}
            argv = plan.job_argv(job("rust"), "n", "net", {"/target": "/target:ro"}, worktree, "7:8", {})
            driver = shell_script(argv)
            result = subprocess.run(["bash", "-c", driver, "nzfleet", "true"], env=env, check=False)
            self.assertEqual(result.returncode, 0)
            with open(handback) as recorded:
                self.assertEqual(recorded.read(), f"-R 7:8 {worktree}\n")

    def test_a_fuzz_job_hands_back_only_its_target_while_a_sibling_compiler_removes_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = os.path.join(temp, "worktree")
            target = os.path.join(temp, "target")
            sibling_file = os.path.join(target, "rust", "debug", "deps", "compiler.tmp")
            own_file = os.path.join(target, "fuzz", "debug", "deps", "fuzzer")
            os.makedirs(os.path.dirname(sibling_file))
            os.makedirs(os.path.dirname(own_file))
            os.makedirs(worktree)
            with open(sibling_file, "w") as file:
                file.write("compiler output")
            with open(own_file, "w") as file:
                file.write("fuzzer output")
            handback = os.path.join(temp, "handback")
            executable = os.path.join(temp, "chown")
            with open(executable, "w") as fake_chown:
                fake_chown.write(
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HANDBACK\"\n"
                    "shift 2\nfor path do find \"$path\" -type f -print; done >> \"$HANDBACK\"\n"
                )
            os.chmod(executable, 0o755)
            env = {**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"], "HANDBACK": handback}
            argv = plan.job_argv(job("fuzz"), "n", "net", {target: "/target", "/repo/.git": "/repo/.git:ro"}, worktree, "7:8", {})
            driver = shell_script(argv).replace("/target", target)
            command = f"rm {shlex.quote(sibling_file)}"
            result = subprocess.run(["bash", "-c", driver, "nzfleet", command], env=env, check=False)
            self.assertEqual(result.returncode, 0)
            with open(handback) as recorded:
                handed_back = recorded.read()
            self.assertIn(f"-R 7:8 {worktree} {target}/fuzz", handed_back)
            self.assertIn(own_file, handed_back)
            self.assertNotIn(f" {target}\n", handed_back)
            self.assertNotIn(sibling_file, handed_back)
            self.assertNotIn("/repo/.git", handed_back)

    def test_a_container_return_traps_failures_and_termination_for_ownership_handoff(self):
        argv = plan.job_argv(job("console"), "n", "net", {}, "/w/x", "7:8", {})
        driver = shell_script(argv)
        self.assertIn("trap finish EXIT", driver)
        self.assertIn("trap 'interrupt 143' TERM", driver)
        self.assertIn("trap 'interrupt 130' INT", driver)

    def test_failed_or_interrupted_checks_preserve_their_status_when_handing_back(self):
        driver = plan.job_argv(job("console"), "n", "net", {}, "/w/x", "7:8", {})
        script = driver[driver.index("-c") + 1]
        for command, status in (("false", 1), ("kill -TERM $PPID", 143), ("kill -INT $PPID", 130)):
            with tempfile.TemporaryDirectory() as temp:
                handoff = os.path.join(temp, "handoff")
                executable = os.path.join(temp, "chown")
                with open(executable, "w") as fake_chown:
                    fake_chown.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HANDOFF\"\n")
                os.chmod(executable, 0o755)
                env = {**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"], "HANDOFF": handoff}
                result = subprocess.run(["bash", "-c", script, "nzfleet", command], env=env, check=False)
                self.assertEqual(result.returncode, status, command)
                with open(handoff) as recorded:
                    self.assertEqual(recorded.read(), "-R 7:8 /w/x\n", command)

    def test_handoff_failure_fails_success_but_preserves_a_failed_check(self):
        driver = plan.job_argv(job("console"), "n", "net", {}, "/w/x", "7:8", {})
        script = driver[driver.index("-c") + 1]
        with tempfile.TemporaryDirectory() as temp:
            handoff = os.path.join(temp, "handoff")
            executable = os.path.join(temp, "chown")
            with open(executable, "w") as fake_chown:
                fake_chown.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HANDOFF\"; exit 9\n")
            os.chmod(executable, 0o755)
            env = {**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"], "HANDOFF": handoff}
            for command, status in (("true", 9), ("false", 1)):
                result = subprocess.run(["bash", "-c", script, "nzfleet", command], env=env, check=False)
                self.assertEqual(result.returncode, status, command)

    def test_term_and_int_stop_a_child_and_grandchild_before_handoff(self):
        driver = plan.job_argv(job("console"), "n", "net", {}, "/w/x", "7:8", {})
        script = driver[driver.index("-c") + 1]
        for sig, status in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as temp:
                child_pipe = os.path.join(temp, "child")
                grandchild_pipe = os.path.join(temp, "grandchild")
                block_pipe = os.path.join(temp, "block")
                handoff = os.path.join(temp, "handoff")
                os.mkfifo(child_pipe)
                os.mkfifo(grandchild_pipe)
                os.mkfifo(block_pipe)
                executable = os.path.join(temp, "chown")
                with open(executable, "w") as fake_chown:
                    fake_chown.write("#!/bin/sh\nprintf '%s\\n' done >> \"$HANDOFF\"\n")
                os.chmod(executable, 0o755)
                env = {**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"], "HANDOFF": handoff,
                       "CHILD_PIPE": child_pipe, "GRANDCHILD_PIPE": grandchild_pipe, "BLOCK_PIPE": block_pipe}
                descriptors = [os.open(path, os.O_RDONLY | os.O_NONBLOCK) for path in (grandchild_pipe, child_pipe)]
                blocker = os.open(block_pipe, os.O_RDWR | os.O_NONBLOCK)
                command = '(exec 8>"$GRANDCHILD_PIPE"; printf x >&8; exec cat <"$BLOCK_PIPE") & '
                command += 'exec 9>"$CHILD_PIPE"; printf x >&9; exec cat <"$BLOCK_PIPE"'
                child = subprocess.Popen(["bash", "-c", script, "nzfleet", command], env=env)
                try:
                    import select
                    for fd in descriptors:
                        self.assertTrue(select.select([fd], [], [], 3)[0], "descendant did not reach its pipe")
                        self.assertEqual(os.read(fd, 1), b"x")
                    child.send_signal(sig)
                    self.assertEqual(child.wait(timeout=3), status)
                    for fd in descriptors:
                        self.assertTrue(select.select([fd], [], [], 3)[0], "descendant still holds its pipe")
                        self.assertEqual(os.read(fd, 1), b"")
                    with open(handoff) as recorded:
                        self.assertEqual(recorded.read(), "done\n")
                finally:
                    for fd in descriptors:
                        os.close(fd)
                    os.close(blocker)
                    if child.poll() is None:
                        child.kill()
                        child.wait()

    def test_the_batch_can_add_to_the_environment_but_not_change_ci_s(self):
        argv = plan.job_argv(job("rust"), "n", "net", {}, "/w", "1:1", {"CARGO_INCREMENTAL": "0"})
        self.assertIn("CARGO_INCREMENTAL=0", argv)
        self.assertIn("NZELA_CARGO=cargo", argv)


class TheSummary(unittest.TestCase):
    def test_a_failed_run_names_the_failed_areas_in_the_form_the_batch_reads_back(self):
        line = plan.summary(["deploy", "rust"], ["deploy"])
        self.assertEqual(line, "failed: deploy")
        self.assertEqual(plan.failed_areas(line, ["deploy", "rust"]), ["deploy"])

    def test_a_clean_run_names_every_area(self):
        self.assertEqual(plan.summary(["deploy", "rust"], []), "passed: deploy rust")


class TheSummaryLine(unittest.TestCase):
    AREAS = ["rust", "console", "deploy"]

    def test_a_test_that_prints_failed_does_not_become_the_summary(self):
        log = "failed: restore drill\n\x1b[1;31mfailed: rust\x1b[0m\nfailed: restore-drill-test\n"
        self.assertEqual(plan.summary_line(log, self.AREAS), "failed: rust")

    def test_the_last_summary_wins_over_an_earlier_one(self):
        self.assertEqual(plan.summary_line("passed: rust\nfailed: console\n", self.AREAS), "failed: console")

    def test_a_log_with_no_summary_gives_an_empty_line(self):
        self.assertEqual(plan.summary_line("failed: restore drill\n", self.AREAS), "")

    def test_an_indented_line_is_not_the_summary(self):
        self.assertEqual(plan.summary_line("  failed: rust\n", self.AREAS), "")

    def test_an_unreadable_summary_leaves_every_area_red(self):
        self.assertEqual(plan.failed_areas(plan.summary_line("failed: restore drill\n", self.AREAS), ["rust", "deploy"]),
                         ["rust", "deploy"])
