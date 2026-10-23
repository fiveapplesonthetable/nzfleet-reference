import os
import tempfile
import unittest
from unittest import mock

from nzfleet import plan, watchdog
from nzfleet.plan import Alarm, Alert

TAIL = [f"line {i}" for i in range(30)]
OPEN = {"number": 7, "body": "old"}


class Reason(unittest.TestCase):
    def test_an_inactive_daemon_is_down_however_fresh_the_log(self):
        self.assertIn("not active", plan.down_reason(False, 1))

    def test_a_log_quiet_for_over_fifteen_minutes_is_down(self):
        self.assertIn("16 minutes", plan.down_reason(True, 16 * 60 + 1))

    def test_a_log_written_fifteen_minutes_ago_is_still_healthy(self):
        self.assertIsNone(plan.down_reason(True, 15 * 60))

    def test_a_missing_log_is_down(self):
        self.assertIn("does not exist", plan.down_reason(True, None))


class Alerts(unittest.TestCase):
    def test_a_first_failure_opens_an_issue_with_the_last_twenty_lines(self):
        alert = plan.down_alert("x", TAIL, None)
        self.assertEqual(alert.kind, Alarm.OPEN)
        self.assertNotIn("line 9\n", alert.body)
        self.assertIn("line 10\n", alert.body)
        self.assertIn("line 29", alert.body)

    def test_a_continuing_failure_updates_the_open_issue_when_the_body_changed(self):
        alert = plan.down_alert("x", TAIL, OPEN)
        self.assertEqual((alert.kind, alert.number), (Alarm.UPDATE, 7))

    def test_a_continuing_failure_with_the_same_body_does_nothing(self):
        same = {"number": 7, "body": plan.down_body("x", TAIL)}
        self.assertEqual(plan.down_alert("x", TAIL, same), Alert(Alarm.NONE))

    def test_recovery_closes_the_open_issue(self):
        self.assertEqual(plan.down_alert(None, TAIL, OPEN), Alert(Alarm.CLOSE, 7))

    def test_health_with_no_issue_does_nothing(self):
        self.assertEqual(plan.down_alert(None, TAIL, None), Alert(Alarm.NONE))


class Shell(unittest.TestCase):
    def test_a_completed_idle_tick_can_write_the_daemon_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "logs", "daemon.heartbeat")
            with mock.patch.object(watchdog, "HEARTBEAT", path):
                watchdog.heartbeat()
                age = watchdog._heartbeat_age()
        self.assertLess(age, 1.0)

    def test_a_fresh_daemon_heartbeat_keeps_an_idle_tick_log_from_opening_an_issue(self):
        with mock.patch.object(watchdog, "_active", return_value=True), \
                mock.patch.object(watchdog, "_heartbeat_age", return_value=1.0), \
                mock.patch.object(watchdog, "_log", return_value=(None, [])), \
                mock.patch.object(watchdog.world, "issue", return_value=None), \
                mock.patch.object(watchdog.world, "set_issue") as opened:
            result = watchdog.check()
        opened.assert_not_called()
        self.assertEqual(result, "none: healthy")

    def test_a_stale_daemon_heartbeat_opens_an_issue_even_when_the_process_is_active(self):
        with mock.patch.object(watchdog, "_active", return_value=True), \
                mock.patch.object(watchdog, "_heartbeat_age", return_value=16 * 60 + 1), \
                mock.patch.object(watchdog, "_log", return_value=(1.0, ["last line"])), \
                mock.patch.object(watchdog.world, "issue", return_value=None), \
                mock.patch.object(watchdog.world, "set_issue") as opened:
            watchdog.check()
        self.assertIn("heartbeat", opened.call_args.args[1])

    def test_a_stopped_daemon_opens_the_issue_through_github(self):
        with mock.patch.object(watchdog, "_active", return_value=False), \
                mock.patch.object(watchdog, "_heartbeat_age", return_value=1.0), \
                mock.patch.object(watchdog, "_log", return_value=(1.0, ["a"])), \
                mock.patch.object(watchdog.world, "issue", return_value=None), \
                mock.patch.object(watchdog.world, "set_issue") as opened:
            watchdog.check()
        self.assertEqual(opened.call_args.args[0], "nzfleet is down")

    def test_a_healthy_daemon_closes_the_issue(self):
        with mock.patch.object(watchdog, "_active", return_value=True), \
                mock.patch.object(watchdog, "_heartbeat_age", return_value=1.0), \
                mock.patch.object(watchdog, "_log", return_value=(1.0, ["a"])), \
                mock.patch.object(watchdog.world, "issue", return_value=OPEN), \
                mock.patch.object(watchdog.world, "close_issue") as closed:
            watchdog.check()
        closed.assert_called_once_with(7)
