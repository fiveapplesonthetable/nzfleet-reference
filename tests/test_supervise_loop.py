import unittest
from unittest import mock

from nzfleet import state, supervise_tick

from fakeworld import World


class TheLoopsSupervision(unittest.TestCase):
    def tick_at(self, w, now):
        host = supervise_tick.Host(now=lambda: now)
        with mock.patch.object(supervise_tick, "Host", return_value=host), \
                mock.patch.object(supervise_tick, "watch_agents") as watch, mock.patch.object(supervise_tick, "hourly") as hourly:
            w.real_supervise(False)
        return watch.call_count, hourly.call_count

    def test_agents_are_sampled_every_tick_and_the_hourly_pass_runs_once_an_hour(self):
        with World() as w:
            with state.change() as s:
                s["supervised"] = 1000.0
            self.assertEqual(self.tick_at(w, 1000.0 + 59 * 60), (1, 0))
            self.assertEqual(self.tick_at(w, 1000.0 + 60 * 60), (1, 1))

    def test_a_dry_run_changes_nothing(self):
        with World() as w:
            with mock.patch.object(supervise_tick, "watch_agents") as watch:
                w.real_supervise(True)
            watch.assert_not_called()

    def test_every_pass_of_the_loop_supervises(self):
        from nzfleet import tick
        with World() as w:
            w.tick()
            tick.supervise.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
