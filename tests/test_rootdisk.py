import tempfile
import unittest
from unittest import mock

from nzfleet import machine, render, rootdisk, watchdog
from test_supervise_tick import Supervised


class Warning(unittest.TestCase):
    def test_percent_rounds_up_so_a_full_disk_is_never_reported_emptier(self):
        self.assertEqual((rootdisk.used_percent(1000, 201), rootdisk.used_percent(1000, 0), rootdisk.used_percent(0, 0)), (80, 100, 0))

    def test_below_eighty_percent_there_is_nothing_to_say(self):
        self.assertIsNone(rootdisk.warning(79, 20))

    def test_from_eighty_percent_the_owner_is_told_how_full_and_how_much_is_left(self):
        self.assertIn("80% full with 20 GB free", rootdisk.warning(80, 20))

    def test_under_twenty_gigabytes_free_the_owner_is_told_whatever_the_percent(self):
        self.assertIn("19 GB free", rootdisk.warning(40, 19))
        self.assertIsNone(rootdisk.warning(40, 20))

    def test_the_warning_comes_well_before_ninety(self):
        self.assertLess(rootdisk.WARN_PERCENT, 90)


class Status(unittest.TestCase):
    def shown(self, percent):
        m = {"load": [1, 1, 1], "cores": 4, "mem_free_gb": 1, "mem_total_gb": 2, "cpu_by_owner": {},
             "disk": [{"mount": "/", "free_gb": 9, "used_percent": percent}]}
        return "\n".join(render.machine(m))

    def test_a_filling_root_disk_is_marked_in_the_status(self):
        self.assertIn("(FILLING)", self.shown(80))

    def test_a_comfortable_root_disk_is_not(self):
        self.assertNotIn("FILLING", self.shown(79))

    def test_the_machine_reading_carries_the_percent_used_of_each_mount(self):
        with tempfile.TemporaryDirectory() as d:
            disk = machine.reading(mounts=(d,))["disk"]
        self.assertEqual([e["mount"] for e in disk], [d])
        self.assertIn("used_percent", disk[0])


class Supervision(Supervised):
    def watch_root(self, usage):
        self.host.root_usage = lambda: usage
        from nzfleet import supervise_tick
        supervise_tick.watch_root_disk(self.host)

    def test_under_forty_gigabytes_free_the_runner_volumes_are_pruned(self):
        ran, told = [], []
        self.host.prune_runner_volumes = lambda: ran.append(1) or "pruned"
        self.host.root_usage = lambda: (85, 39)
        from nzfleet import supervise_tick
        supervise_tick.watch_root_disk(self.host, told.append)
        self.assertEqual((ran, told), ([1], ["pruned"]))

    def test_with_forty_gigabytes_free_nothing_is_pruned(self):
        self.host.prune_runner_volumes = lambda: self.fail("pruned a disk with room")
        self.watch_root((50, 40))

    def test_a_filling_disk_is_put_to_the_owner(self):
        self.watch_root((85, 30))
        from nzfleet import state
        self.assertIn("85%", state.read()["needs_person"]["disk:root"])

    def test_the_request_goes_once_the_disk_is_comfortable(self):
        from nzfleet import state
        self.watch_root((85, 30))
        self.watch_root((40, 300))
        self.assertNotIn("disk:root", state.read()["needs_person"])


class Watchdog(unittest.TestCase):
    def run_check(self, usage, found):
        with mock.patch.object(watchdog.world, "issue", return_value=found), \
                mock.patch.object(watchdog.world, "set_issue") as opened, \
                mock.patch.object(watchdog.world, "close_issue") as closed:
            watchdog.root_disk(lambda: usage)
        return opened, closed

    def test_a_filling_disk_opens_the_issue(self):
        opened, _ = self.run_check((90, 10), None)
        self.assertEqual(opened.call_args.args[0], rootdisk.TITLE)

    def test_a_disk_with_room_closes_the_issue(self):
        _, closed = self.run_check((10, 400), {"number": 9, "body": "old"})
        closed.assert_called_once_with(9)

    def test_a_disk_with_room_and_no_issue_does_nothing(self):
        opened, closed = self.run_check((10, 400), None)
        opened.assert_not_called()
        closed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
