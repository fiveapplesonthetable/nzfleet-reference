import datetime
import unittest

from nzfleet import event_log

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)
LOG = ["2026-10-10 15:00:00 old", "2026-10-10 17:30:00 recent", "continuation of recent", "2026-10-10 17:59:00 newest"]


class SelectingEvents(unittest.TestCase):
    def test_since_an_age_keeps_only_newer_lines(self):
        since = event_log.parse_since("1h", NOW)
        self.assertEqual(event_log.select(LOG, since, 20), LOG[1:])

    def test_a_line_without_a_stamp_follows_the_line_before_it(self):
        self.assertIn("continuation of recent", event_log.select(LOG, event_log.parse_since("1h", NOW), 20))

    def test_without_since_it_takes_the_last_n(self):
        self.assertEqual(event_log.select(LOG, None, 2), LOG[-2:])

    def test_a_date_and_time_is_read_as_utc(self):
        self.assertEqual(event_log.parse_since("2026-10-10 17:00", NOW), datetime.datetime(2026, 10, 10, 17, tzinfo=UTC))

    def test_nonsense_is_refused(self):
        with self.assertRaises(ValueError):
            event_log.parse_since("yesterday-ish", NOW)
