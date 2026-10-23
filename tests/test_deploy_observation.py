import contextlib
import unittest
from unittest.mock import patch

import yaml

from nzfleet import tick


class DeployObservation(unittest.TestCase):
    def setUp(self):
        self.saved = {"deploys": {}, "deploy_runs": [], "deploy_started": [], "digest": []}
        self.events = []
        self.comments = []
        self.runs = [
            {"databaseId": 31, "headSha": "main-sha", "status": "completed", "conclusion": "success"},
        ]
        self.jobs = {31: {"Confirm successful main push": "success", "Deploy API and background services": "success",
                          "Deploy admin console": "success", "Deploy website and signed Android app": "success"}}
        self.patches = [
            patch.object(tick.world, "main_sha", return_value="main-sha"),
            patch.object(tick.world, "is_ancestor", side_effect=lambda sha, main: sha == main),
            patch.object(tick.world, "deploy_runs", side_effect=lambda: self.runs),
            patch.object(tick.world, "deploy_jobs", side_effect=lambda run_id: self.jobs[run_id]),
            patch.object(tick.state, "read", side_effect=lambda: self.saved),
            patch.object(tick.state, "change", side_effect=self.change),
            patch.object(tick, "tell_event", side_effect=self.events.append),
            patch.object(tick.world, "issue", return_value={"number": 811}),
            patch.object(tick.world, "comment", side_effect=lambda number, body: self.comments.append(body)),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    @contextlib.contextmanager
    def change(self):
        yield self.saved

    def test_a_successful_workflow_is_reported_once_without_a_second_roll(self):
        with patch.object(tick.cloud, "login", side_effect=AssertionError("unexpected authentication")), \
                patch.object(tick.cloud, "live", side_effect=AssertionError("unexpected Cloud Run query")):
            tick.deploy(False)
            tick.deploy(False)
        self.assertEqual(self.saved["deploys"]["main-sha"]["run_id"], 31)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.comments), 1)
        self.assertIn("deployed", self.events[0])

    def test_a_failed_workflow_is_reported_as_failed(self):
        self.runs[0].update(databaseId=32, conclusion="failure")
        self.jobs[32] = {"Confirm successful main push": "success", "Deploy API and background services": "success",
                          "Deploy admin console": "failure", "Deploy website and signed Android app": "success"}
        tick.deploy(False)
        self.assertFalse(self.saved["deploys"]["main-sha"]["ok"])
        self.assertIn("console", self.events[0])
        self.assertIn("Deploy failed", self.comments[0])

    def test_two_runs_of_one_commit_are_each_reported_once(self):
        self.runs.insert(0, {"databaseId": 32, "headSha": "main-sha", "status": "completed", "conclusion": "failure"})
        self.jobs[32] = {"Confirm successful main push": "success", "Deploy API and background services": "failure",
                          "Deploy admin console": "success", "Deploy website and signed Android app": "success"}
        tick.deploy(False)
        tick.deploy(False)
        self.assertEqual(self.saved["deploy_runs"], [31, 32])
        self.assertEqual(self.saved["deploys"]["main-sha"]["run_id"], 32)
        self.assertEqual(len(self.events), 2)

    def test_first_observation_reports_only_the_latest_gated_run(self):
        self.saved["deploy_runs"] = None
        self.runs.insert(0, {"databaseId": 32, "headSha": "main-sha", "status": "completed", "conclusion": "failure"})
        self.jobs[32] = {"Confirm successful main push": "success", "Deploy API and background services": "failure",
                          "Deploy admin console": "success", "Deploy website and signed Android app": "success"}
        tick.deploy(False)
        tick.deploy(False)
        self.assertEqual(self.saved["deploy_runs"], [31, 32])
        self.assertEqual(len(self.events), 1)
        self.assertIn("32", self.events[0])

    def test_a_skipped_or_untrusted_run_is_not_reported(self):
        self.runs.extend([
            {"databaseId": 32, "headSha": "main-sha", "status": "completed", "conclusion": "skipped"},
            {"databaseId": 33, "headSha": "other-sha", "status": "completed", "conclusion": "failure"},
            {"databaseId": 34, "headSha": "main-sha", "status": "in_progress", "conclusion": ""},
        ])
        self.jobs[31]["Confirm successful main push"] = "skipped"
        self.jobs[34] = {"Confirm successful main push": "skipped"}
        tick.deploy(False)
        self.assertEqual(self.events, [])

    def test_a_deploy_whose_gate_fails_is_reported(self):
        self.runs[0].update(databaseId=35, conclusion="failure")
        self.jobs[35] = {"Confirm successful main push": "failure"}
        tick.deploy(False)
        self.assertFalse(self.saved["deploys"]["main-sha"]["ok"])
        self.assertIn("Confirm successful main push", self.events[0])
        self.assertIn("Deploy failed", self.comments[0])


class ReleaseWorkflow(unittest.TestCase):
    def test_the_sole_release_path_signs_and_checks_the_served_apk(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[3] / ".github/workflows/deploy.yaml"
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        self.assertEqual(set(jobs) & {"deploy", "console", "site"}, {"deploy", "console", "site"})
        site_steps = "\n".join(str(step) for step in jobs["site"]["steps"])
        self.assertIn('"$build_tools/apksigner" sign', site_steps)
        self.assertIn("/download/nzela.apk", site_steps)

    def test_scheduled_storage_tidy_reports_failures_and_closes_them_after_recovery(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[3] / ".github/workflows/actions-tidy.yaml"
        parsed = yaml.safe_load(workflow.read_text())
        self.assertIn("issues", parsed["permissions"])
        report = next(step for step in parsed["jobs"]["tidy"]["steps"] if step.get("name") == "Report the result")
        self.assertEqual(report["if"], "always()")
        self.assertIn("gh issue create", report["run"])
        self.assertIn("gh issue close", report["run"])

    def test_offsite_timer_services_report_failures(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "deploy/systemd"
        for name in ("nzela-offsite-backup.service", "nzela-offsite-restore-check.service"):
            self.assertIn("OnFailure=nzela-offsite-report@%n.service", (root / name).read_text())
        self.assertTrue((root / "nzela-offsite-report@.service").is_file())


if __name__ == "__main__":
    unittest.main()
