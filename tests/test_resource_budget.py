"""Properties of the CI host CPU allocation."""

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/root/resource_budget.py"
SPEC = importlib.util.spec_from_file_location("resource_budget", SCRIPT)
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)
WORKFLOW = (ROOT.parents[1] / ".github/workflows/ci.yaml").read_text()
QUOTAS = budget.configured_quotas()


def edit_job(workflow, job, pattern, replacement, section=None):
    """Rewrite one job's text with a pattern that must match, so a reformat fails here instead of passing as a no-op."""
    body = budget.job_section(workflow, job)
    target = body
    if section:
        head, sep, tail = body.partition(section[0])
        target = head if section[1] == "head" else tail
    changed, count = re.subn(pattern, replacement, target, count=1)
    if count != 1:
        raise AssertionError(f"anchor {pattern!r} not found in job {job}")
    return workflow.replace(body, body.replace(target, changed, 1), 1)


def container_cpus(workflow, job, cpus):
    return edit_job(workflow, job, r"(options: \$\{\{ vars.CI_RUNS_ON && '--cpus=)\d+(')", rf"\g<1>{cpus}\2",
                    section=("    services:", "head"))


def postgres_edit(workflow, job, pattern, replacement):
    return edit_job(workflow, job, pattern, replacement, section=("    services:", "tail"))


class ResourceBudget(unittest.TestCase):
    def test_console_ci_jobs_run_their_matching_checks_areas(self):
        workflow = yaml.safe_load(WORKFLOW)
        checks = (ROOT.parents[1] / "scripts/checks.sh").read_text()
        for area in ("console", "console-browser"):
            with self.subTest(area=area):
                job = workflow["jobs"][area]
                self.assertEqual(job["timeout-minutes"], 30)
                runs = [step.get("run", "") for step in job["steps"]]
                check_area = "console-static" if area == "console" else area
                self.assertTrue(any(run.endswith(f"scripts/checks.sh {check_area}") for run in runs))
                self.assertRegex(checks, rf"(?m)^\s*{re.escape(check_area)}\)\s.*\bcheck_{check_area.replace('-', '_')}\b")

    def test_quotas_and_headroom_fit_the_effective_host(self):
        trusted, shared, limits = budget.plan(budget.cpus("0-71"), WORKFLOW, QUOTAS)
        self.assertEqual(len(trusted), 36)
        self.assertEqual(len(shared), 36)
        trusted_quota, owner, pr, batch, agent, rust, console, console_static, android, image, services, light = limits
        self.assertEqual((trusted_quota, owner, pr, batch, agent), tuple(QUOTAS[k] for k in ("trusted", "owner", "pr", "batch", "agent")))
        self.assertEqual(rust, tuple(budget.job_cpus(WORKFLOW, job) for job in ("check", "compat", "e2e")))
        self.assertEqual((console, console_static, android),
                         (budget.job_cpus(WORKFLOW, "console-browser"), budget.job_cpus(WORKFLOW, "console"), budget.android_cpus(WORKFLOW)))
        self.assertLessEqual(rust[0] + sum(rust[1:]) + console_static + android + image + services + light, trusted_quota)

    def test_the_owner_keeps_twenty_four_cpus_and_the_pr_tier_twenty(self):
        self.assertEqual((QUOTAS["owner"], QUOTAS["pr"]), (24, 20))

    def test_the_owner_slice_outweighs_the_pull_request_slice(self):
        weights = budget.configured_weights()
        self.assertGreater(weights["owner"], weights["pr"])
        with self.assertRaisesRegex(ValueError, "owner slice must weigh more"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, QUOTAS, weights={"owner": 100, "pr": 100})

    def test_a_batch_or_agent_that_could_fill_the_owner_slice_is_refused(self):
        for key in ("batch", "agent"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "interactive headroom"):
                    budget.plan(budget.cpus("0-71"), WORKFLOW, {**QUOTAS, key: QUOTAS["owner"]})

    def test_small_host_refuses_an_overcommitted_partition(self):
        with self.assertRaisesRegex(ValueError, "effective affinity|aggregate quotas"):
            budget.plan(budget.cpus("0-62"), WORKFLOW, QUOTAS)

    def test_configured_partition_is_checked_on_a_hosted_runner(self):
        configured_host = set(range(QUOTAS["trusted"] + max(QUOTAS["owner"], QUOTAS["pr"]) + 4))
        trusted, shared, _ = budget.plan(configured_host, WORKFLOW, QUOTAS)
        self.assertEqual((len(trusted), len(shared)), (36, 28))

    def test_installation_still_refuses_a_four_cpu_host(self):
        with self.assertRaisesRegex(ValueError, "effective affinity"):
            budget.plan(budget.cpus("0-3"), WORKFLOW, QUOTAS)

    def test_hosted_validation_accepts_four_cpu_runner_without_weakening_installation(self):
        configured = subprocess.run(
            [sys.executable, str(SCRIPT), "--host-cpus", "0-3", "--configured"],
            capture_output=True, text=True, check=False,
        )
        installation = subprocess.run(
            [sys.executable, str(SCRIPT), "--host-cpus", "0-3"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertIn("trusted=0-35 shared=36-63", configured.stdout)
        self.assertEqual(installation.returncode, 2, installation.stderr)
        self.assertIn("CPU quota exceeds effective affinity", installation.stderr)

    def test_configured_plan_keeps_hosted_ci_caps_and_affinity_validation(self):
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, {**QUOTAS, "trusted": 8})

    def test_quota_larger_than_effective_affinity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "effective affinity"):
            budget.validate(budget.cpus("0-71"), budget.cpus("0-22"), budget.cpus("23-71"), WORKFLOW, QUOTAS)

    def test_overlapping_affinity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "affinity domains"):
            budget.validate(budget.cpus("0-71"), budget.cpus("0-39"), budget.cpus("32-71"), WORKFLOW, QUOTAS)

    def test_extra_ci_container_budget_consumes_android_and_service_headroom(self):
        overloaded = container_cpus(WORKFLOW, "console-browser", QUOTAS["trusted"] // 3 + 1)
        with self.assertRaisesRegex(ValueError, "console browser cap"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_console_browser_budget_is_counted_separately(self):
        overloaded = container_cpus(WORKFLOW, "console-browser", QUOTAS["trusted"] // 3 + 1)
        with self.assertRaisesRegex(ValueError, "console browser cap"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_android_forks_are_part_of_the_closed_trusted_budget(self):
        overloaded = edit_job(WORKFLOW, "android-compile", r"ActiveProcessorCount=\d+", "ActiveProcessorCount=16")
        overloaded = edit_job(overloaded, "android-compile", r"org.gradle.workers.max=\d+", "org.gradle.workers.max=16")
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_android_cpu_view_and_worker_cap_must_agree(self):
        overloaded = edit_job(WORKFLOW, "android-compile", r"ActiveProcessorCount=\d+", "ActiveProcessorCount=16")
        with self.assertRaisesRegex(ValueError, "Android processor view"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_release_peak_counts_contract_and_site_when_they_can_overlap(self):
        overlapping = edit_job(WORKFLOW, "android-release", r"ActiveProcessorCount=\d+", "ActiveProcessorCount=3")
        overlapping = edit_job(overlapping, "android-release", r"org.gradle.workers.max=\d+", "org.gradle.workers.max=3")
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), overlapping, QUOTAS)

    def test_release_peak_excludes_only_predecessors_it_waits_for(self):
        faster = edit_job(WORKFLOW, "android-release", r"ActiveProcessorCount=\d+", "ActiveProcessorCount=3")
        faster = edit_job(faster, "android-release", r"org.gradle.workers.max=\d+", "org.gradle.workers.max=3")
        site_only = edit_job(faster, "android-release", r"needs: select\b", "needs: [select, site]")
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), site_only, QUOTAS)
        serial = edit_job(faster, "android-release", r"needs: select\b", "needs: [select, contract, site]")
        serial = edit_job(serial, "android-shard", r"needs: \[select, android-compile\]", "needs: [select, android-compile, compat]")
        trusted, _, _ = budget.plan(budget.cpus("0-71"), serial, QUOTAS)
        self.assertEqual(len(trusted), 36)

    def test_release_image_runs_beside_android(self):
        overlapping = edit_job(WORKFLOW, "image", r"needs: select\b", "needs: [select, android]")
        with self.assertRaisesRegex(ValueError, "release image must run in parallel"):
            budget.plan(budget.cpus("0-71"), overlapping, QUOTAS)

    def test_device_gradle_waits_for_heavy_jobs(self):
        overlapping = edit_job(WORKFLOW, "android-device", r"needs: \[[^\]]*\]", "needs: select")
        with self.assertRaisesRegex(ValueError, "device work must wait"):
            budget.plan(budget.cpus("0-71"), overlapping, QUOTAS)

    def test_postgres_without_a_cap_is_refused(self):
        uncapped = postgres_edit(WORKFLOW, "check", r"\$\{\{ vars.CI_RUNS_ON && '--cpus=\d+' \|\| '' \}\}", "")
        with self.assertRaisesRegex(ValueError, "Postgres lacks"):
            budget.plan(budget.cpus("0-71"), uncapped, QUOTAS)

    def test_rust_postgres_has_capacity_for_parallel_database_lifecycles(self):
        for pattern, replacement in ((r"'--cpus=\d+'", "'--cpus=1'"),
                                     (r"--memory=\d+g", "--memory=4g"),
                                     (r"--memory=\d+g", "")):
            with self.subTest(replacement=replacement):
                underprovisioned = postgres_edit(WORKFLOW, "check", pattern, replacement)
                with self.assertRaisesRegex(ValueError, "Rust Postgres needs"):
                    budget.plan(budget.cpus("0-71"), underprovisioned, QUOTAS)

    def test_rust_postgres_cannot_exceed_the_closed_trusted_budget(self):
        overloaded = postgres_edit(WORKFLOW, "check", r"'--cpus=\d+'", f"'--cpus={QUOTAS['trusted']}'")
        with self.assertRaisesRegex(ValueError, "trusted parent has"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_build_and_test_threads_match_container_quota(self):
        mismatch = edit_job(WORKFLOW, "compat", r'RUST_TEST_THREADS: "\d+"', 'RUST_TEST_THREADS: "1"')
        with self.assertRaisesRegex(ValueError, "RUST_TEST_THREADS"):
            budget.plan(budget.cpus("0-71"), mismatch, QUOTAS)

    def test_old_partition_cannot_run_the_ci_job_budget(self):
        old = {"trusted": 8, "owner": 28, "pr": 12, "batch": 16, "agent": 12, "background": 4}
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, old)

    def test_agent_and_batch_share_the_owner_ceiling(self):
        over = {**QUOTAS, "agent": 25}
        with self.assertRaisesRegex(ValueError, "interactive headroom"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, over)

    def test_background_load_shares_the_owner_ceiling(self):
        over = {**QUOTAS, "background": QUOTAS["owner"] + 1}
        with self.assertRaisesRegex(ValueError, "interactive headroom"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, over)

    def test_the_pull_request_tier_holds_its_heaviest_job(self):
        self.assertGreaterEqual(QUOTAS["pr"], budget.heaviest_job(WORKFLOW))
        with self.assertRaisesRegex(ValueError, "heaviest job declares"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, {**QUOTAS, "pr": 8, "owner": 16})

    def test_the_pull_request_tier_has_twenty_cpus_and_the_owner_twenty_four(self):
        self.assertEqual((QUOTAS["pr"], QUOTAS["owner"], QUOTAS["background"]), (20, 24, 4))

    def test_host_reserve_is_four_cpus(self):
        with self.assertRaisesRegex(ValueError, "aggregate quotas"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, QUOTAS, reserves=13)

    def test_rust_shards_wait_for_the_archive_and_cover_every_partition(self):
        jobs = yaml.safe_load(WORKFLOW)["jobs"]
        self.assertIn("rust-archive", jobs["rust-tests"]["needs"])
        self.assertEqual(jobs["rust-tests"]["strategy"]["matrix"]["shard"], ["1/4", "2/4", "3/4", "4/4"])
