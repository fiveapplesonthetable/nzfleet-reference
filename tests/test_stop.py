"""A worker a person stopped stays stopped: reaping does not call it unfinished and resuming does not bring it back."""

import json
import unittest
from unittest import mock

from nzfleet import agents, attention, machine, procs, runs, state

from fleet import Fleet, NOW


class Stopping(Fleet):
    def stop(self, key="app-data"):
        with mock.patch.object(machine, "stop_unit") as unit, mock.patch.object(procs, "kill_group"):
            why = agents.stop(key, NOW)
        return why, unit

    def test_a_stopped_run_is_not_resumed_after_it_is_reaped(self):
        self.start()
        self.stop()
        self.die("app-data", NOW)
        agents.resume(now=NOW + 10 ** 6)
        self.assertEqual(self.agents()["app-data"]["status"], "stopped")
        self.assertNotIn("app-data~2", self.agents())

    def test_stopping_a_run_stops_its_unit(self):
        self.start()
        unit = self.agents()["app-data"]["unit"]
        _, stopped = self.stop()
        stopped.assert_called_once_with(unit)

    def test_a_stopped_task_waits_for_a_person(self):
        self.start()
        self.stop()
        self.assertEqual(runs.run_state("app-data", self.agents()), "stuck")

    def test_the_latest_run_of_a_task_is_the_one_stopped(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.stop("app-data")
        self.assertEqual(self.agents()["app-data~2"]["status"], "stopped")
        self.assertEqual(self.agents()["app-data"]["status"], "unfinished")

    def test_a_task_with_no_running_run_cannot_be_stopped(self):
        self.start()
        self.die("app-data", NOW, "PR: #12\n")
        why, _ = self.stop()
        self.assertIn("not running", why)
        self.assertEqual(self.agents()["app-data"]["status"], "done")

    def test_a_task_nobody_started_cannot_be_stopped(self):
        why, _ = self.stop("nothing")
        self.assertIn("no run", why)

    def test_the_supervisors_own_stop_is_still_resumed(self):
        self.start()
        agents.mark_stopped("app-data", "stopped: idle", False, NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertIn("app-data~2", self.agents())

    def test_state_written_before_the_status_existed_reads_the_same(self):
        self.start()
        self.die("app-data", NOW)
        old = json.loads(json.dumps(state.read()))
        self.assertNotIn("stopped", {a["status"] for a in old["agents"].values()})
        self.assertEqual(runs.run_state("app-data", old["agents"]), "resuming")


class GivingUp(Fleet):
    def test_a_resume_the_dispatcher_refuses_is_not_retried_every_tick(self):
        self.start()
        self.die("app-data", NOW)
        with mock.patch.object(agents.world, "pull_facts"), mock.patch.object(agents.dispatch, "refusal", return_value="#7 is closed"):
            with state.change() as s:
                s["agents"]["app-data"]["for_pr"] = 7
            agents.resume(now=NOW + runs.BACKOFF[0])
            self.assertEqual(self.agents()["app-data"]["status"], runs.ABANDONED)
            before = len(self.launched)
            agents.resume(now=NOW + 2 * runs.BACKOFF[0])
        self.assertEqual(len(self.launched), before)
        self.assertEqual(runs.run_state("app-data", self.agents()), "stuck")

    def test_a_run_with_no_resumptions_left_stops_being_unfinished(self):
        self.start()
        self.die("app-data", NOW)
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.die("app-data~2", NOW)
        agents.resume(now=NOW + 10 ** 6)
        self.assertEqual(self.agents()["app-data~2"]["status"], runs.ABANDONED)
        self.assertEqual([s.key for s in runs.stuck_runs(self.agents(), NOW)], ["app-data"])

    def test_state_with_an_exhausted_unfinished_run_is_still_reported_stuck(self):
        old = {"a": {"status": "unfinished", "finished": 1}, "a~2": {"status": "unfinished", "finished": 2}}
        self.assertEqual([s.key for s in runs.stuck_runs(old, 5)], ["a"])


class AttentionLines(Fleet):
    def test_an_idle_workers_fix_is_the_stop_command(self):
        self.start()
        with state.change() as s:
            s["agents"]["app-data"]["pid"] = 4321
        s = state.read()
        lines = attention.needs_attention(s, [], {}, {}, {"app-data": (500.0, 1)}, NOW)
        fixes = {line["fix"] for line in lines if "app-data" in line["text"]}
        self.assertEqual(fixes, {"nzfleet stop app-data"})


if __name__ == "__main__":
    unittest.main()
