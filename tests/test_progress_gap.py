import unittest
from unittest import mock

from nzfleet import supervise_tick


class TheProgressRows(unittest.TestCase):
    def test_skip_a_task_whose_only_record_is_a_later_run(self):
        s = {"agents": {"app-2~2": {"status": "done", "finished": 0}}, "samples": {}, "todo": [], "batches": {}, "passed": {}, "merged_at": {}}
        host = mock.Mock(now=lambda: 0)
        agent_rows, _ = supervise_tick.rows(s, host, {})
        self.assertEqual(agent_rows, [])


if __name__ == "__main__":
    unittest.main()
