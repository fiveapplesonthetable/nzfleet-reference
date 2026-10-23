import subprocess
import os
import tempfile
import unittest
from unittest import mock

from nzfleet import starve, state


class Planning(unittest.TestCase):
    def plan(self, **kw):
        defaults = dict(total_cpus=72, size=starve.SLICE, cpu_set=None, taskset=True, scope=True)
        return starve.plan_starve(**{**defaults, **kw})

    def test_the_default_slice_is_four_cpus_leaving_the_first_ones_to_the_machine(self):
        plan = self.plan()
        self.assertEqual(plan.cpus, "68-71")
        self.assertEqual(plan.burners, 16)

    def test_burners_are_four_times_the_size_of_the_slice(self):
        self.assertEqual(self.plan(size=4).burners, 16)

    def test_a_small_fleet_gets_at_most_one_eighth_of_its_cpus(self):
        plan = self.plan(total_cpus=8)
        self.assertEqual((plan.cpus, plan.burners), ("7", 4))

    def test_an_explicit_cpu_set_sets_the_burner_count_by_its_size(self):
        plan = self.plan(cpu_set="0-3,8")
        self.assertEqual((plan.cpus, plan.burners), ("0-3,8", 20))

    def test_a_cpu_set_that_is_not_a_cpu_list_is_refused(self):
        for bad in ("", "a-b", "5-2", "1,,2", "-3"):
            self.assertIsNone(self.plan(cpu_set=bad).wrap, bad)
            self.assertTrue(self.plan(cpu_set=bad).problem, bad)

    def test_a_cpu_beyond_the_machine_is_refused(self):
        self.assertIn("available affinity", self.plan(cpu_set="60-80").problem)

    def test_a_cpu_set_larger_than_one_eighth_of_the_fleet_is_refused(self):
        self.assertIn("one eighth", self.plan(cpu_set="0-9").problem)

    def test_an_empty_requested_slice_is_refused(self):
        self.assertIn("at least one CPU", self.plan(size=0).problem)

    def test_the_default_slice_uses_the_highest_available_ids_when_affinity_is_sparse(self):
        plan = starve.plan_starve(16, 4, None, True, True, [0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19, 24, 25, 30, 31])
        self.assertEqual((plan.cpus, plan.burners), ("30-31", 8))

    def test_an_explicit_slice_outside_process_affinity_is_refused(self):
        plan = starve.plan_starve(16, 2, "4-5", True, True, [0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19, 24, 25, 30, 31])
        self.assertIn("available affinity", plan.problem)

    def test_taskset_pins_the_command(self):
        self.assertEqual(self.plan().wrap, ["taskset", "-c", "68-71"])

    def test_a_systemd_scope_with_allowed_cpus_is_used_when_there_is_no_taskset(self):
        wrap = self.plan(taskset=False).wrap
        self.assertEqual(wrap[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertIn("AllowedCPUs=68-71", wrap)

    def test_with_neither_there_is_a_sentence_and_no_command(self):
        plan = self.plan(taskset=False, scope=False)
        self.assertIsNone(plan.wrap)
        self.assertIn("taskset", plan.problem)


class Running(unittest.TestCase):
    def setUp(self):
        self.spawned, self.stopped, self.called, self.said = [], [], [], []
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        for name, value in (("PATH", os.path.join(home.name, "state.json")), ("LOCK", os.path.join(home.name, "state.lock"))):
            patch = mock.patch.object(state, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def spawn(self, argv, **kw):
        self.spawned.append((argv, kw))
        return type("P", (), {"pid": 1000 + len(self.spawned)})()

    def call(self, argv, **kw):
        self.called.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.code)

    def starve_it(self, code=0, command=("cargo", "test")):
        self.code = code
        plan = starve.plan_starve(72, starve.SLICE, None, True, True)
        return starve.run(list(command), plan, spawn=self.spawn, call=self.call, stop=self.stopped.append, say=self.said.append)

    def test_the_command_runs_pinned_to_the_slice_and_marked_as_a_starvation_run(self):
        self.starve_it()
        (argv, kw), = self.called
        self.assertEqual(argv, ["taskset", "-c", "68-71", "cargo", "test"])
        self.assertEqual(kw["env"]["NZFLEET_STARVE"], "1")
        self.assertEqual(kw["timeout"], starve.MAX_CALL_SECONDS)

    def test_four_times_the_slice_in_burners_are_started_in_the_slice_and_marked(self):
        self.starve_it()
        self.assertEqual(len(self.spawned), 16)
        argv, kw = self.spawned[0]
        self.assertEqual(argv[:3], ["taskset", "-c", "68-71"])
        self.assertEqual(argv[-1], "yes")
        self.assertEqual(kw["env"]["NZFLEET_STARVE"], "1")

    def test_every_burner_is_stopped_when_the_command_ends(self):
        self.starve_it()
        self.assertEqual(sorted(self.stopped), [1000 + n for n in range(1, 17)])

    def test_the_commands_exit_code_is_returned_and_burners_stop_when_it_fails(self):
        self.assertEqual(self.starve_it(code=101), 101)
        self.assertEqual(len(self.stopped), 16)

    def test_burners_stop_when_the_command_cannot_be_started(self):
        def broken(argv, **kw):
            raise FileNotFoundError(argv[3])
        plan = starve.plan_starve(72, starve.SLICE, None, True, True)
        self.assertEqual(starve.run(["nope"], plan, spawn=self.spawn, call=broken, stop=self.stopped.append, say=self.said.append), 127)
        self.assertEqual(len(self.stopped), 16)

    def test_a_running_batch_refuses_starve_before_starting_any_burner(self):
        with state.change() as data:
            data["batches"]["manual"] = {"status": "running", "pid": 2**22 + 1}
        plan = starve.plan_starve(72, starve.SLICE, None, True, True)
        self.assertEqual(starve.run(["cargo", "test"], plan, spawn=self.spawn, call=self.call,
                                    stop=self.stopped.append, say=self.said.append), 1)
        self.assertEqual((self.spawned, self.called), ([], []))
        self.assertIn("merge-check batch is running", self.said[-1])

    def test_a_command_that_exceeds_five_minutes_is_stopped(self):
        def timed_out(argv, **kw):
            self.called.append((argv, kw))
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        plan = starve.plan_starve(72, starve.SLICE, None, True, True)
        self.assertEqual(starve.run(["cargo", "test"], plan, spawn=self.spawn, call=timed_out,
                                    stop=self.stopped.append, say=self.said.append), 124)
        self.assertEqual(len(self.stopped), 16)

    def test_a_timed_out_command_has_its_process_group_killed_and_reaped(self):
        command = ["cargo", "test"]
        process = mock.Mock(pid=456, returncode=-9)
        process.communicate.side_effect = subprocess.TimeoutExpired(command, starve.MAX_CALL_SECONDS)
        with mock.patch.object(starve.subprocess, "Popen", return_value=process), \
             mock.patch.object(starve.procs, "kill_group") as kill_group:
            with self.assertRaises(subprocess.TimeoutExpired):
                starve._bounded_call(command, {}, starve.MAX_CALL_SECONDS)
        kill_group.assert_called_once_with(456, now=True)
        process.wait.assert_called_once_with()

    def test_a_plan_with_a_problem_starts_nothing(self):
        plan = starve.plan_starve(72, 16, None, False, False)
        self.assertEqual(starve.run(["cargo"], plan, spawn=self.spawn, call=self.call, stop=self.stopped.append, say=self.said.append), 1)
        self.assertEqual((self.spawned, self.called), ([], []))


if __name__ == "__main__":
    unittest.main()
