import copy
import unittest
from contextlib import contextmanager
from unittest import mock

from nzfleet import owner_tick, render, state, tick


class MainHealth(unittest.TestCase):
    def observe(self, jobs, previous=None, now=1000.0):
        stored = copy.deepcopy(previous or {"main_ci": {}, "main_ci_current": {}, "main_reported": [], "main_code_failures": {}, "failed_jobs_told": []})
        calls = []

        @contextmanager
        def change():
            yield stored

        with mock.patch.object(tick.world, "main_sha", return_value="main-sha") as sha, \
                mock.patch.object(tick.world, "ci_jobs", return_value=jobs) as ci, \
                mock.patch.object(state, "read", return_value=stored), \
                mock.patch.object(state, "change", side_effect=change), \
                mock.patch.object(tick.time, "time", return_value=now), \
                mock.patch.object(tick, "tell_event"), mock.patch.object(tick, "tell_news"), \
                mock.patch.object(owner_tick.mail, "send", return_value=True), \
                mock.patch.object(tick, "say"), mock.patch.object(tick.world, "job_log", return_value=""), \
                mock.patch.object(tick.plan, "classify", return_value="infrastructure"):
            tick.main_health(False)
        calls.extend((sha.call_count, ci.call_count))
        return stored, calls

    def observe_twice(self, first, second):
        stored = {"main_ci": {}, "main_ci_current": {}, "main_reported": [], "main_code_failures": {}, "failed_jobs_told": []}
        sent, logs = [], []

        @contextmanager
        def change():
            yield stored

        def send(subject, body):
            sent.append(subject)
            return True

        with mock.patch.object(tick.world, "main_sha", return_value="main-sha"), \
                mock.patch.object(tick.world, "ci_jobs", side_effect=[first, second]), \
                mock.patch.object(state, "read", return_value=stored), \
                mock.patch.object(state, "change", side_effect=change), \
                mock.patch.object(tick, "tell_event"), mock.patch.object(tick, "say"), \
                mock.patch.object(tick.world, "job_log", side_effect=lambda run, job: logs.append(job) or "setup\nerror[E0308]: mismatched types\n"), \
                mock.patch.object(owner_tick.mail, "send", side_effect=send):
            tick.main_health(False)
            tick.main_health(False)
        return stored, sent, logs

    def test_a_failed_job_is_named_while_the_run_is_still_going(self):
        stored, _, _ = self.observe_twice(("in_progress", {"rust": "failure", "console": None}, 50),
                                          ("in_progress", {"rust": "failure", "console": None}, 50))
        self.assertEqual(stored["main_ci_current"]["failed_jobs"], {"rust": "error[E0308]: mismatched types"})
        self.assertEqual(stored["main_ci_current"]["conclusion"], "pending")

    def test_a_failed_job_is_mailed_once_not_every_tick(self):
        _, sent, logs = self.observe_twice(("in_progress", {"rust": "failure"}, 50), ("in_progress", {"rust": "failure"}, 50))
        self.assertEqual(sent, ["nzela: main main-sha failed rust"])
        self.assertEqual(logs, ["rust"])

    def test_a_second_job_failing_later_is_mailed_on_its_own(self):
        _, sent, _ = self.observe_twice(("in_progress", {"rust": "failure", "console": None}, 50),
                                        ("in_progress", {"rust": "failure", "console": "failure"}, 50))
        self.assertEqual(sent, ["nzela: main main-sha failed rust", "nzela: main main-sha failed console"])

    def test_the_status_lists_a_failed_job_of_a_running_main(self):
        main = {"sha": "abc12345", "status": "in_progress", "green": None, "run_id": 5, "failed_jobs": {"rust": "error: boom"}}
        out = render.pipeline({"main": main, "queue": "", "batches": [], "train_prs": [], "chains": [], "waiting": [], "train_refused": ""})
        self.assertIn("  main job failed: rust: error: boom", out)

    def test_latest_pending_run_keeps_a_previous_failure_as_history(self):
        previous = {"main_ci": {"sha": "older", "ok": False, "since": 100.0}, "main_ci_current": {},
                    "main_reported": ["older"], "main_code_failures": {}}
        stored, calls = self.observe(("in_progress", {}, 41), previous)
        self.assertEqual(calls, [1, 1])
        self.assertEqual(stored["main_ci"], previous["main_ci"])
        self.assertEqual(stored["main_ci_current"], {"sha": "main-sha", "status": "in_progress", "conclusion": "pending", "run_id": 41, "first_observed_at": 1000.0, "at": 1000.0})

    def test_pending_after_green_keeps_the_completed_green_result(self):
        previous = {"main_ci": {"sha": "older", "ok": True, "since": 100.0}, "main_reported": [], "main_code_failures": {}, "failed_jobs_told": []}
        stored, _ = self.observe(("queued", {}, 42), previous)
        self.assertEqual(stored["main_ci"]["ok"], True)
        self.assertEqual(stored["main_ci_current"]["conclusion"], "pending")

    def test_a_completed_failure_is_kept_as_history_and_reported_once(self):
        stored, _ = self.observe(("completed", {"rust": "failure"}, 43))
        self.assertEqual(stored["main_ci"], {"sha": "main-sha", "run_id": 43, "ok": False, "since": 1000.0, "completed_at": 1000.0})
        self.assertEqual(stored["main_ci_current"]["conclusion"], "failure")
        self.assertEqual(stored["main_reported"], ["main-sha"])

    def test_an_unknown_api_result_does_not_replace_the_last_completed_result(self):
        previous = {"main_ci": {"sha": "older", "ok": False, "since": 100.0}, "main_reported": [], "main_code_failures": {}, "failed_jobs_told": []}
        stored, _ = self.observe(None, previous)
        self.assertEqual(stored["main_ci"], previous["main_ci"])
        self.assertEqual(stored["main_ci_current"]["status"], "unknown")

    def test_an_unrecognized_api_run_status_stays_unknown(self):
        stored, _ = self.observe(("future_status", {}, 46), {"main_ci": {"sha": "old", "ok": True, "since": 100.0}})
        self.assertEqual((stored["main_ci_current"]["status"], stored["main_ci_current"]["conclusion"]), ("unknown", "unknown"))
        self.assertEqual(stored["main_ci"]["sha"], "old")

    def test_cancelled_or_superseded_runs_are_not_reported_as_code_failures(self):
        stored, _ = self.observe(("completed", {"rust": "cancelled"}, 44))
        self.assertEqual((stored["main_ci_current"]["status"], stored["main_ci_current"]["conclusion"]), ("cancelled", "cancelled"))
        self.assertNotIn("main-sha", stored["main_reported"])
        self.assertEqual(stored["main_ci"], {})

    def test_a_startup_failure_is_a_known_completed_failure(self):
        stored, _ = self.observe(("completed", {"rust": "startup_failure"}, 45))
        self.assertEqual((stored["main_ci_current"]["status"], stored["main_ci_current"]["conclusion"]), ("completed", "failure"))
        self.assertFalse(stored["main_ci"]["ok"])

    def test_a_timed_out_latest_run_is_a_current_failure_and_completed_history(self):
        previous = {"main_ci": {"sha": "green-sha", "ok": True, "since": 400.0}, "main_ci_current": {},
                    "main_reported": [], "main_code_failures": {}, "failed_jobs_told": []}
        stored, _ = self.observe(("completed", {"rust": "timed_out"}, 47), previous)
        self.assertEqual((stored["main_ci_current"]["conclusion"], stored["main_ci_current"]["status"]), ("failure", "completed"))
        self.assertEqual(stored["main_ci"], {"sha": "main-sha", "run_id": 47, "ok": False, "since": 1000.0, "completed_at": 1000.0})

    def test_a_live_observation_age_keeps_its_first_timestamp_and_resets_for_a_new_run(self):
        first, _ = self.observe(("in_progress", {}, 48))
        second, _ = self.observe(("in_progress", {}, 48), first, now=1100.0)
        self.assertEqual(second["main_ci_current"]["first_observed_at"], 1000.0)
        self.assertEqual(second["main_ci_current"]["at"], 1100.0)
        third, _ = self.observe(("queued", {}, 49), second, now=1200.0)
        self.assertEqual(third["main_ci_current"]["first_observed_at"], 1200.0)

    def test_a_new_completed_sha_has_its_own_completion_time_when_outcome_stays_red(self):
        previous = {"main_ci": {"sha": "old", "ok": False, "since": 200.0, "completed_at": 300.0},
                    "main_ci_current": {}, "main_reported": ["old"], "main_code_failures": {}, "failed_jobs_told": []}
        stored, _ = self.observe(("completed", {"rust": "failure"}, 50), previous, now=1200.0)
        self.assertEqual(stored["main_ci"], {"sha": "main-sha", "run_id": 50, "ok": False, "since": 200.0, "completed_at": 1200.0})
        repeated, _ = self.observe(("completed", {"rust": "failure"}, 50), stored, now=1400.0)
        self.assertEqual(repeated["main_ci"]["completed_at"], 1200.0)


if __name__ == "__main__":
    unittest.main()
