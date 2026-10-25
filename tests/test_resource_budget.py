"""Properties of the CI host CPU allocation."""

import importlib.util
from pathlib import Path
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
                self.assertIn(f"{check_area}) check_{check_area.replace('-', '_')} || status=1 ;;", checks)

    def test_quotas_and_headroom_fit_the_effective_host(self):
        trusted, shared, limits = budget.plan(budget.cpus("0-71"), WORKFLOW, QUOTAS)
        self.assertEqual(len(trusted), 36)
        self.assertEqual(len(shared), 36)
        self.assertEqual(limits, (36, 24, 20, 12, 12, (7, 3, 3), 12, 2, 6, 4, 2, 5))

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
        overloaded = WORKFLOW.replace("options: ${{ vars.CI_RUNS_ON && '--cpus=12' || '' }}", "options: ${{ vars.CI_RUNS_ON && '--cpus=17' || '' }}", 1)
        with self.assertRaisesRegex(ValueError, "console browser cap"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_console_browser_budget_is_counted_separately(self):
        overloaded = WORKFLOW.replace("options: ${{ vars.CI_RUNS_ON && '--cpus=12' || '' }}", "options: ${{ vars.CI_RUNS_ON && '--cpus=17' || '' }}", 1)
        with self.assertRaisesRegex(ValueError, "console browser cap"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_android_forks_are_part_of_the_closed_trusted_budget(self):
        overloaded = WORKFLOW.replace('ActiveProcessorCount=6', 'ActiveProcessorCount=16').replace('org.gradle.workers.max=6', 'org.gradle.workers.max=16')
        with self.assertRaisesRegex(ValueError, "CI jobs, Android"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_android_cpu_view_and_worker_cap_must_agree(self):
        overloaded = WORKFLOW.replace('ActiveProcessorCount=6', 'ActiveProcessorCount=16')
        with self.assertRaisesRegex(ValueError, "Android processor view"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_release_image_runs_beside_android(self):
        overlapping = WORKFLOW.replace('needs: select\n    outputs:\n      image_id:', 'needs: [select, android]\n    outputs:\n      image_id:')
        with self.assertRaisesRegex(ValueError, "release image must run in parallel"):
            budget.plan(budget.cpus("0-71"), overlapping, QUOTAS)

    def test_device_gradle_waits_for_heavy_jobs(self):
        overlapping = WORKFLOW.replace('needs: [select, check, compat, e2e, console, console-browser, android, image]', 'needs: select')
        with self.assertRaisesRegex(ValueError, "device work must wait"):
            budget.plan(budget.cpus("0-71"), overlapping, QUOTAS)

    def test_postgres_without_a_cap_is_refused(self):
        uncapped = WORKFLOW.replace("${{ vars.CI_RUNS_ON && '--cpus=4' || '' }}", "", 1)
        with self.assertRaisesRegex(ValueError, "Postgres lacks"):
            budget.plan(budget.cpus("0-71"), uncapped, QUOTAS)

    def test_rust_postgres_has_capacity_for_parallel_database_lifecycles(self):
        for original, replacement in (("'--cpus=4'", "'--cpus=1'"),
                                      ("--memory=8g", "--memory=4g"),
                                      ("--memory=8g", "")):
            with self.subTest(replacement=replacement):
                underprovisioned = WORKFLOW.replace(original, replacement, 1)
                with self.assertRaisesRegex(ValueError, "Rust Postgres needs"):
                    budget.plan(budget.cpus("0-71"), underprovisioned, QUOTAS)

    def test_rust_postgres_cannot_exceed_the_closed_trusted_budget(self):
        overloaded = WORKFLOW.replace("'--cpus=4'", "'--cpus=5'", 1)
        with self.assertRaisesRegex(ValueError, "need 37 CPUs, trusted parent has 36"):
            budget.plan(budget.cpus("0-71"), overloaded, QUOTAS)

    def test_build_and_test_threads_match_container_quota(self):
        mismatch = WORKFLOW.replace('RUST_TEST_THREADS: "3"', 'RUST_TEST_THREADS: "2"', 1)
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
        with self.assertRaisesRegex(ValueError, "heaviest job declares 12"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, {**QUOTAS, "pr": 8, "owner": 16})

    def test_the_pull_request_tier_has_twenty_cpus_and_the_owner_twenty_four(self):
        self.assertEqual((QUOTAS["pr"], QUOTAS["owner"], QUOTAS["background"]), (20, 24, 4))

    def test_host_reserve_is_four_cpus(self):
        with self.assertRaisesRegex(ValueError, "aggregate quotas"):
            budget.plan(budget.cpus("0-71"), WORKFLOW, QUOTAS, reserves=13)
