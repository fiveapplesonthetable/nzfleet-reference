import unittest
from unittest import mock

from nzfleet import mail


class OwnerNotification(unittest.TestCase):
    def test_an_unconfigured_notifier_is_logged_without_retrying(self):
        with mock.patch.object(mail, "NOTIFY", ""):
            with mock.patch.object(mail.subprocess, "run") as run:
                with self.assertLogs(level="WARNING") as messages:
                    self.assertTrue(mail.send("subject", "body"))
                run.assert_not_called()
        self.assertEqual(messages.output, [
            "WARNING:root:NZFLEET_NOTIFY is not configured; owner notification was not sent"
        ])
