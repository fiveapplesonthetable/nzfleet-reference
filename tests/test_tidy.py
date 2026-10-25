import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from nzfleet import cli, tidy
from nzfleet.tidy import Checkout, Container, Network, Sized, Snapshot, Tree

HOUR = 3600


def removed(snap):
    return [getattr(a, "path", None) or a.name for a in tidy.decide(snap)]


class Caches(unittest.TestCase):
    def test_a_cache_over_its_cap_goes(self):
        self.assertEqual(removed(Snapshot(caches=(Sized("/h/sccache", 81, 80),))), ["/h/sccache"])

    def test_a_cache_at_its_cap_stays(self):
        self.assertEqual(removed(Snapshot(caches=(Sized("/h/sccache", 80, 80),))), [])

    def test_a_slots_build_directory_a_batch_holds_stays_however_big(self):
        self.assertEqual(removed(Snapshot(caches=(Sized("/h/warm/slot-1/target", 200, 80, held=True),))), [])


class BuildOutput(unittest.TestCase):
    def test_a_build_dir_untouched_for_ninety_minutes_goes(self):
        self.assertEqual(removed(Snapshot(builds=(Tree("/dev/shm/nzt/a", 90 * 60),))), ["/dev/shm/nzt/a"])

    def test_a_build_dir_touched_a_minute_ago_stays(self):
        self.assertEqual(removed(Snapshot(builds=(Tree("/dev/shm/nzt/a", 60),))), [])

    def test_a_build_dir_a_process_is_building_into_stays_however_quiet(self):
        snap = Snapshot(builds=(Tree("/dev/shm/nzt/a", 5 * HOUR),), held_paths=frozenset({"/dev/shm/nzt/a/debug"}))
        self.assertEqual(removed(snap), [])

    def test_a_build_dir_named_by_a_live_process_argument_stays(self):
        snap = Snapshot(builds=(Tree("/dev/shm/nzt/a", 5 * HOUR),), held_args=("--out-dir=/dev/shm/nzt/a/debug",))
        self.assertEqual(removed(snap), [])

    def test_a_build_dir_of_a_worktree_with_a_process_in_it_stays(self):
        snap = Snapshot(builds=(Tree("/dev/shm/nzt/slug", 5 * HOUR),), starts=(("slug", "/work/slug"),),
                        held_paths=frozenset({"/work/slug/crates"}))
        self.assertEqual(removed(snap), [])

    def test_a_build_dir_of_a_running_batch_stays(self):
        snap = Snapshot(builds=(Tree("/var/tmp/nzt/nzf-manual-7-r1", 5 * HOUR),), batches=frozenset({"manual-7"}))
        self.assertEqual(removed(snap), [])

    def test_a_build_dir_of_a_finished_batch_goes(self):
        snap = Snapshot(builds=(Tree("/var/tmp/nzt/nzf-manual-6-r1", 5 * HOUR),), batches=frozenset({"manual-7"}))
        self.assertEqual(removed(snap), ["/var/tmp/nzt/nzf-manual-6-r1"])


class Containers(unittest.TestCase):
    def snap(self, container, **more):
        return Snapshot(containers=(container,), **more)

    def test_an_old_agent_database_with_no_client_goes(self):
        self.assertEqual(removed(self.snap(Container("agentdb-a", 3 * HOUR, 0, 5000))), ["agentdb-a"])

    def test_one_a_client_is_connected_to_stays(self):
        self.assertEqual(removed(self.snap(Container("agentdb-a", 3 * HOUR, 1, 5000))), [])

    def test_one_started_an_hour_ago_stays(self):
        self.assertEqual(removed(self.snap(Container("nzela-hook-pg-1", HOUR, 0, 5000))), [])

    def test_one_a_live_process_has_in_its_database_url_stays(self):
        self.assertEqual(removed(self.snap(Container("agentdb-a", 3 * HOUR, 0, 5000), held_ports=frozenset({5000}))), [])

    def test_the_database_of_a_worktree_with_a_process_in_it_stays(self):
        snap = self.snap(Container("agentdb-slug", 3 * HOUR, 0, 5001), starts=(("slug", "/work/slug"),),
                         held_paths=frozenset({"/work/slug"}))
        self.assertEqual(removed(snap), [])

    def test_the_database_of_a_batch_that_ended_goes_even_with_a_client_still_connected(self):
        self.assertEqual(removed(self.snap(Container("nzf-core-100-pg", 30 * 60, 2, 5000))), ["nzf-core-100-pg"])

    def test_the_database_of_a_batch_still_running_stays(self):
        snap = self.snap(Container("nzf-core-100-pg", 5 * HOUR, 0, 5000), batches=frozenset({"core-100"}))
        self.assertEqual(removed(snap), [])

    def test_the_database_of_a_batch_that_started_minutes_ago_stays(self):
        self.assertEqual(removed(self.snap(Container("nzf-core-100-pg", 10 * 60, 0, 5000))), [])

    def test_a_container_that_is_not_ours_is_never_touched(self):
        self.assertEqual(removed(self.snap(Container("redis", 9 * HOUR, 0, None))), [])


class Worktrees(unittest.TestCase):
    OLD = Checkout("/work/a", 7 * HOUR)

    def test_an_idle_clean_pushed_worktree_goes(self):
        self.assertEqual(removed(Snapshot(checkouts=(self.OLD,))), ["/work/a"])

    def test_a_worktree_with_no_live_batch_is_removed_however_dirty(self):
        dirty = Checkout("/work/nzf-manual-5-r1", 7 * HOUR)
        self.assertEqual(removed(Snapshot(checkouts=(dirty,))), ["/work/nzf-manual-5-r1"])

    def test_one_with_a_process_in_it_stays(self):
        self.assertEqual(removed(Snapshot(checkouts=(self.OLD,), held_paths=frozenset({"/work/a/frontend"}))), [])

    def test_a_running_batchs_worktree_stays(self):
        old = Checkout("/work/nzf-manual-7-r1", 7 * HOUR)
        self.assertEqual(removed(Snapshot(checkouts=(old,), batches=frozenset({"manual-7"}))), [])


class Unpacks(unittest.TestCase):
    def test_only_a_day_old_runtime_goes(self):
        snap = Snapshot(unpacks=(Tree("/tmp/robolectric1", 25 * HOUR), Tree("/tmp/robolectric2", 23 * HOUR)))
        self.assertEqual(removed(snap), ["/tmp/robolectric1"])


class Temporaries(unittest.TestCase):
    OLD = 5 * HOUR

    def test_files_untouched_for_hours_go(self):
        self.assertEqual(removed(Snapshot(temps=(Tree("/t/app/x", self.OLD),))), ["/t/app/x"])

    def test_files_touched_within_the_window_stay(self):
        self.assertEqual(removed(Snapshot(temps=(Tree("/t/app/x", 3 * HOUR),))), [])

    def test_files_a_live_process_has_open_stay_however_old(self):
        snap = Snapshot(temps=(Tree("/t/app/x", self.OLD),), held_paths=frozenset({"/t/app/x/lib.so"}))
        self.assertEqual(removed(snap), [])

    def test_a_directory_a_live_process_names_as_its_tmpdir_stays(self):
        snap = Snapshot(temps=(Tree("/t/app/x", self.OLD),), scratch=frozenset({"/t/app"}))
        self.assertEqual(removed(snap), [])

    def test_robolectric_files_in_use_stay_past_a_day(self):
        snap = Snapshot(unpacks=(Tree("/tmp/robolectric1", 30 * HOUR),), held_paths=frozenset({"/tmp/robolectric1/a.jar"}))
        self.assertEqual(removed(snap), [])


class Schedule(unittest.TestCase):
    def test_it_is_due_every_fifteen_minutes(self):
        self.assertFalse(tidy.due(1000, 1000 + 14 * 60))
        self.assertTrue(tidy.due(1000, 1000 + 15 * 60))

    def test_stale_run_candidates_exclude_live_processes_and_open_prs(self):
        runs = {"old": {"status": "unfinished", "finished": 100.0},
                "live": {"status": "unfinished", "finished": 100.0},
                "pr": {"status": "unfinished", "finished": 100.0, "pr": {"number": 9}},
                "branch": {"status": "unfinished", "finished": 100.0, "branch": "open-branch"},
                "recent": {"status": "unfinished", "finished": 90000.0}}
        self.assertEqual(tidy.stale_runs(runs, 100000.0, 1, {"live"}, {9, "open-branch"}), ["old"])

    def test_tidy_help_exposes_the_stale_run_cutoff(self):
        output = StringIO()
        with self.assertRaises(SystemExit) as result, redirect_stdout(output):
            cli.main(["tidy", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--stale-runs", output.getvalue())

    def test_stale_run_tidy_command_retires_an_eligible_run(self):
        state = {"agents": {"old": {"status": "unfinished", "finished": 100.0, "task": "old", "unit": "nzf-agent-old-1"}},
                 "todo": [{"key": "old", "task": "old"}], "task_history": {}, "worktrees": {}}

        @contextmanager
        def change():
            yield state

        output = StringIO()
        with redirect_stdout(output), mock.patch.object(cli.sys.stdout, "reconfigure", create=True), \
                mock.patch.object(cli, "warn_if_behind"), \
                mock.patch.object(cli.state, "read", return_value=state), \
                mock.patch.object(cli.state, "change", change), \
                mock.patch.object(cli.world, "open_prs", return_value=[]), \
                mock.patch.object(cli.agents, "_alive", return_value=False), \
                mock.patch.object(cli.machine, "stop_unit") as stop, \
                mock.patch("nzfleet.cli.time.time", return_value=100000.0):
            self.assertEqual(cli.main(["tidy", "--stale-runs", "1"]), 0)
        stop.assert_called_once_with("nzf-agent-old-1")
        self.assertEqual(state["agents"]["old"]["status"], "retired")
        self.assertEqual(state["todo"], [])
        self.assertEqual(state["task_history"]["old"]["status"], "retired")
        self.assertIn("retired 1 stale run", output.getvalue())


if __name__ == "__main__":
    unittest.main()


class Networks(unittest.TestCase):
    def decided(self, networks, batches=()):
        return [a for a in tidy.decide(Snapshot(networks=networks, batches=frozenset(batches)))
                if isinstance(a, tidy.RemoveNetwork)]

    def test_a_network_of_a_batch_that_is_gone_is_removed_with_the_containers_on_it(self):
        found = self.decided((Network("nzf-manual-6-r1-rust", ("nzf-manual-6-r1-rust-service",)),), batches={"manual-7"})
        self.assertEqual([(a.name, a.containers) for a in found],
                         [("nzf-manual-6-r1-rust", ("nzf-manual-6-r1-rust-service",))])

    def test_a_network_of_a_running_batch_stays(self):
        self.assertEqual(self.decided((Network("nzf-manual-7-r1-rust", ()),), batches={"manual-7"}), [])

    def test_a_network_that_is_not_a_batchs_is_never_touched(self):
        self.assertEqual(self.decided((Network("bridge", ()), Network("agentdb-net", ()))), [])

    def test_a_brand_new_network_of_a_dead_batch_goes_too_because_age_is_not_asked(self):
        self.assertEqual(len(self.decided((Network("nzf-manual-6-r1-rust", ()),))), 1)
