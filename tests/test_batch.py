import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from nzfleet import batch, config, plan, refresh, state, tick, world

FORMAL_PROOF_FILES = [".github/workflows/red.yaml", "formal/Transfer.tla"]

class FakeChecks:
    """Merge-checks where a run fails if it holds a broken member, or fails once if `flaky` is set."""

    def __init__(self, broken=(), flaky=False, infrastructure=False, skip=(), unrunnable_after=None):
        self.broken, self.flaky, self.infrastructure, self.skip = set(broken), flaky, infrastructure, set(skip)
        self.unrunnable_after = unrunnable_after
        self.calls, self.areas = [], []

    def __call__(self, name, areas, members):
        numbers = [n for n, _ in members if n not in self.skip]
        self.calls.append(numbers)
        self.areas.append(list(areas))
        if self.unrunnable_after is not None and len(self.calls) > self.unrunnable_after:
            return {"rc": 92, "merged": [], "skipped": [], "line": "merge-check left no result", "log": ""}
        if self.infrastructure:
            return self._result(1, numbers, "No space left on device")
        if self.flaky:
            self.flaky = False
            return self._result(1, numbers, "test worker::tests::slow ... FAILED")
        bad = set(numbers) & self.broken
        return self._result(1 if bad else 0, numbers, "test money::tests::adds ... FAILED" if bad else "passed: rust")

    def _result(self, rc, merged, log):
        return {"rc": rc, "merged": merged, "skipped": sorted(self.skip), "line": log.splitlines()[0], "log": log,
                "stats": {area: {"rc": rc} for area in (self.areas[-1] if self.areas else [])}}


class Runs(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        state.PATH = os.path.join(self.home, "state.json")
        state.LOCK = os.path.join(self.home, "state.lock")
        main = mock.patch.object(world, "MAIN", "HEAD")
        main.start()
        self.addCleanup(main.stop)

    def test_a_batch_classifies_areas_with_the_fleets_own_rules_and_needs_no_checkout_of_main(self):
        self.assertEqual(world.areas([".github/workflows/fuzz.yaml"]), frozenset({"secrets", "deploy"}))

    def test_a_batch_ends_after_its_wall_clock_however_many_rounds_remain(self):
        clock = iter((0, 8 * 3600, 8 * 3600 + 1))
        self.assertFalse(batch.wall_clock_expired(0, next(clock), 8 * 3600))
        self.assertTrue(batch.wall_clock_expired(0, next(clock), 8 * 3600))

        self.start([1])
        now = [0]

        def slow_check(name, areas, members):
            now[0] = 8 * 3600
            return FakeChecks()._result(0, [1], "passed")

        batch.run("b", areas_of=lambda shas: frozenset(), check=slow_check, clock=lambda: now[0])
        self.assertEqual(state.read()["batches"]["b"]["status"], "wall-clock")
        self.assertEqual(len(state.read()["batches"]["b"].get("tests", [])), 0)

    def start(self, members, batch_id="b", main="m", group="core"):
        with state.change() as s:
            s["batches"][batch_id] = {"group": group, "members": [[n, f"s{n}"] for n in members], "areas": ["rust"],
                                 "main": main, "started": 0, "status": "running", "pid": 0}

    def test_a_green_batch_passes_every_member_in_order(self):
        self.start([3, 1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks())
        s = state.read()
        self.assertEqual(s["batches"]["b"]["status"], "green")
        self.assertEqual(s["passed"]["2"], {"sha": "s2", "main": "m", "order": [3, 1, 2], "batch": "b"})

    def test_a_single_failed_member_is_recorded_after_one_check(self):
        self.start([1327])
        fake = FakeChecks(broken={1327})
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        record = state.read()
        self.assertEqual(fake.calls, [[1327]])
        self.assertEqual(record["batches"]["b"]["status"], "red")
        self.assertEqual(record["batches"]["b"]["passed"], [])
        self.assertEqual(record["failed"]["1327"]["sha"], "s1327")
        self.assertEqual(record["passed"], {})
        self.assertEqual(record["flakes"], {})

    def test_a_single_failed_member_cannot_turn_green_on_a_second_check(self):
        self.start([1327])
        fake = FakeChecks(flaky=True)
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        record = state.read()
        self.assertEqual(fake.calls, [[1327]])
        self.assertEqual(record["batches"]["b"]["status"], "red")
        self.assertEqual(record["passed"], {})
        self.assertNotIn("proved", record["batches"]["b"])

    def test_a_screenshot_commit_does_not_extend_proof_to_an_unchecked_tree(self):
        self.start([1, 2])
        shots = os.path.join(self.home, "shots")
        os.makedirs(os.path.join(shots, "console"))
        open(os.path.join(shots, "console", "changed.txt"), "w").write("new\tfrontend/admin/screenshots/07-senders.png\t\n")
        check = FakeChecks()

        def with_shots(name, areas, members):
            return {**check(name, areas, members), "shots": shots, "tree": "plain"}

        with mock.patch.object(refresh, "commit_train", return_value="pushed") as commit, \
                mock.patch.object(refresh, "golden", return_value=set()), \
                mock.patch.object(world, "verified_tree", return_value="shotstree") as verified_tree, \
                mock.patch.object(refresh, "unowned", return_value=[]), \
                mock.patch.object(world, "branch_of", side_effect=lambda n: f"b{n}"):
            batch.run("b", areas_of=lambda shas: frozenset(), check=with_shots)
        b = state.read()["batches"]["b"]
        self.assertEqual(b["status"], "shots-updated")
        self.assertEqual([c.args[:2] + c.args[3:4] for c in commit.call_args_list], [("s2", shots, "b2")])
        self.assertNotIn("2", state.read()["passed"])
        self.assertEqual(b["invalidated_heads"], {"2": "pushed"})
        self.assertNotIn("proved", b)
        verified_tree.assert_not_called()
        self.assertIn("committed on #2", b["line"])
        self.assertIn("1 shot moved: 07-senders", b["line"])
        self.assertIn(os.path.join(shots, "console", "index.html"), b["line"])

    def test_the_broken_member_is_blamed_and_the_rest_pass(self):
        self.start([1, 2, 3, 4, 5])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(broken={4}))
        s = state.read()
        self.assertEqual(set(s["failed"]), {"4"})
        self.assertEqual(s["failed"]["4"]["tests"], ["money::tests::adds"])
        self.assertEqual(s["batches"]["b"]["passed"], [1, 2, 3, 5])

    def test_a_failure_that_does_not_repeat_is_a_flake_not_a_culprit(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(flaky=True))
        s = state.read()
        self.assertEqual(s["failed"], {})
        self.assertEqual(s["batches"]["b"]["flaky"], ["worker::tests::slow"])

    def test_a_failed_manual_batch_is_reported_once_and_never_run_again(self):
        self.start([1, 2, 3], group="manual")
        fake = FakeChecks(broken={2})
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        b = state.read()["batches"]["b"]
        self.assertEqual((len(fake.calls), b["status"], state.read()["failed"]), (1, "red-once", {}))
        self.assertIn("FAILED", b["line"])

    def test_a_daemon_batch_still_reruns_and_bisects(self):
        self.start([1, 2, 3])
        fake = FakeChecks(broken={2})
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        self.assertGreater(len(fake.calls), 1)
        self.assertEqual(set(state.read()["failed"]), {"2"})

    def test_a_daemon_batch_is_not_run_again_while_a_newer_batch_covers_one_of_its_prs(self):
        self.start([1, 2, 3])
        self.start([3, 4], "newer")
        with state.change() as s:
            s["batches"]["newer"]["started"] = 5
        fake = FakeChecks(broken={2})
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        self.assertEqual((len(fake.calls), state.read()["batches"]["b"]["status"]), (1, "red-once"))

    def test_a_newer_batch_over_other_prs_does_not_stop_the_rerun(self):
        self.start([1, 2, 3])
        self.start([7, 8], "newer")
        with state.change() as s:
            s["batches"]["newer"]["started"] = 5
        fake = FakeChecks(broken={2})
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        self.assertEqual(set(state.read()["failed"]), {"2"})

    def test_a_flake_is_in_the_ledger_with_its_run(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(flaky=True))
        entry = state.read()["flakes"]["worker::tests::slow"]
        self.assertEqual((entry["count"], entry["runs"]), (1, [os.path.join(batch.RUNS, "b-r0")]))

    def test_the_second_flake_of_a_test_queues_one_task_and_the_third_none(self):
        for batch_id in ("b", "c", "d"):
            self.start([1, 2], batch_id, main=f"m-{batch_id}")
            batch.run(batch_id, areas_of=lambda shas: frozenset(), check=FakeChecks(flaky=True))
        s = state.read()
        self.assertEqual(s["flakes"]["worker::tests::slow"]["count"], 3)
        self.assertEqual([t["key"] for t in s["todo"]], ["flake-worker-tests-slow"])

    def test_a_counterexample_after_a_flake_does_not_count_for_the_test(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(flaky=True))
        self.start([1, 2], "c")
        fake = FakeChecks(flaky=True)
        real = fake._result
        fake._result = lambda rc, merged, log: real(rc, merged, log + ("\nminimal failing input: (1, 2)" if rc else ""))
        batch.run("c", areas_of=lambda shas: frozenset(), check=fake)
        s = state.read()
        self.assertEqual((s["flakes"]["worker::tests::slow"]["count"], s["todo"]), (1, []))

    @mock.patch.object(config, "SKIP_AREAS", frozenset())
    def test_a_rerun_covers_only_the_areas_that_failed(self):
        self.start([1, 2])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["rust", "console", "formal"]
        fake = FakeChecks(flaky=True)
        fake._result = lambda rc, merged, log: {"rc": rc, "merged": merged, "skipped": [],
                                                "line": "failed: console" if rc else "passed: console", "log": log}
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        self.assertEqual(fake.areas, [["console", "rust"], ["console"]])

    def test_a_flaky_area_completes_the_proof_only_when_both_runs_share_a_tree(self):
        self.start([1, 2])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["console", "rust"]
        calls = 0

        def check(name, areas, members):
            nonlocal calls
            calls += 1
            return {"rc": 1 if calls == 1 else 0, "merged": [1, 2], "skipped": [],
                    "line": "failed: console" if calls == 1 else "passed: console",
                    "log": "test dashboard FAILED" if calls == 1 else "", "tree": "tree-a" if calls == 1 else "tree-b",
                    "stats": {area: {"rc": 1 if area == "console" and calls == 1 else 0} for area in areas}}

        batch.run("b", areas_of=lambda shas: frozenset(), check=check)
        record = state.read()["batches"]["b"]
        self.assertEqual(record["checked_areas"], ["console", "rust"])
        self.assertEqual(record["status"], "incomplete-proof")
        self.assertEqual(record["proved"], {"tree": "tree-b", "areas": ["console"], "runs": {"console": "b-r0-again"}})

    def test_a_flaky_area_completes_the_full_proof_when_both_runs_share_a_tree(self):
        self.start([1, 2])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["console", "rust"]
        calls = 0

        def check(name, areas, members):
            nonlocal calls
            calls += 1
            return {"rc": 1 if calls == 1 else 0, "merged": [1, 2], "skipped": [],
                    "line": "failed: console" if calls == 1 else "passed: console",
                    "log": "test dashboard FAILED" if calls == 1 else "", "tree": "same-tree",
                    "stats": {area: {"rc": 1 if area == "console" and calls == 1 else 0} for area in areas}}

        batch.run("b", areas_of=lambda shas: frozenset(), check=check)
        self.assertEqual(state.read()["batches"]["b"]["proved"],
                         {"tree": "same-tree", "areas": ["console", "rust"],
                          "runs": {"console": "b-r0-again", "rust": "b-r0"}})

    def test_a_compile_retry_proves_only_areas_with_a_recorded_success(self):
        self.start([1, 2])
        requested = ["compile", "android", "console", "rust"]
        with state.change() as s:
            s["batches"]["b"]["areas"] = requested
        calls = []

        def check(name, areas, members):
            calls.append(list(areas))
            if len(calls) == 1:
                return {"rc": 1, "merged": [1, 2], "skipped": [], "line": "failed: compile", "log": "compile failed",
                        "tree": "same-tree", "stats": {"compile": {"rc": 1}}}
            return {"rc": 0, "merged": [1, 2], "skipped": [], "line": "passed: compile android console rust", "log": "passed",
                    "tree": "same-tree", "stats": {"compile": {"rc": 0}}}

        batch.run("b", areas_of=lambda shas: frozenset(), check=check)
        self.assertEqual(calls, [sorted(requested), sorted(requested)])
        record = state.read()["batches"]["b"]
        self.assertEqual(record["status"], "incomplete-proof")
        self.assertEqual(record["proved"], {"tree": "same-tree", "areas": ["compile"], "runs": {"compile": "b-r0-again"}})

    def test_a_zero_exit_without_all_area_results_is_not_a_complete_pass(self):
        self.start([1])
        requested = ["compile", "android", "console"]
        with state.change() as s:
            s["batches"]["b"]["areas"] = requested
        fake = FakeChecks()
        fake._result = lambda rc, merged, log: {"rc": 0, "merged": merged, "skipped": [], "line": "passed",
                                                "log": log, "tree": "tree-a", "stats": {"compile": {"rc": 0}}}
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        record = state.read()["batches"]["b"]
        self.assertEqual(record["status"], "incomplete-proof")
        self.assertEqual(record["proved"], {"tree": "tree-a", "areas": ["compile"], "runs": {"compile": "b-r0"}})
        self.assertEqual(record["passed"], [])
        self.assertIn("android console", record["line"])

    def test_a_property_counterexample_is_never_a_flake(self):
        self.start([1, 2])
        fake = FakeChecks(flaky=True)
        real = fake._result
        fake._result = lambda rc, merged, log: real(rc, merged, log + ("\nminimal failing input: (1, 2)" if rc else ""))
        batch.run("b", areas_of=lambda shas: frozenset(), check=fake)
        s = state.read()
        self.assertEqual((s["batches"]["b"]["status"], s["passed"], s["failed"], s["flakes"]), ("counterexample", {}, {}, {}))

    def test_the_areas_checked_include_what_the_heads_touch(self):
        self.start([1])
        fake = FakeChecks()
        batch.run("b", check=fake, areas_of=lambda shas: frozenset({"android"}))
        self.assertIn("android", fake.areas[0])

    @mock.patch.object(config, "SKIP_AREAS", frozenset({"formal"}))
    @mock.patch.object(world, "changed_files", return_value=FORMAL_PROOF_FILES)
    @mock.patch.object(world, "areas", new=lambda files, tier="change": frozenset({"deploy", "secrets"}))
    def test_formal_source_changes_do_not_add_a_train_area(self, changed_files):
        self.start([921])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["deploy", "secrets"]
        fake = FakeChecks()
        batch.run("b", check=fake)
        self.assertNotIn("formal", fake.areas[0])
        self.assertNotIn("formal", state.read()["batches"]["b"]["checked_areas"])
        self.assertIn("921", state.read()["passed"])

    @mock.patch.object(config, "SKIP_AREAS", frozenset({"formal"}))
    @mock.patch.object(world, "changed_files", return_value=FORMAL_PROOF_FILES)
    @mock.patch.object(world, "areas", new=lambda files, tier="change": frozenset({"deploy", "secrets"}))
    def test_a_queued_formal_area_is_removed_before_a_batch(self, changed_files):
        self.start([921])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["deploy", "formal", "secrets"]
        fake = FakeChecks()

        batch.run("b", check=fake)

        self.assertEqual(fake.areas, [["deploy", "secrets"]])
        self.assertNotIn("formal", state.read()["batches"]["b"]["checked_areas"])
        self.assertIn("921", state.read()["passed"])

    @mock.patch.object(config, "SKIP_AREAS", frozenset({"formal"}))
    @mock.patch.object(world, "changed_files", return_value=FORMAL_PROOF_FILES)
    @mock.patch.object(world, "areas", new=lambda files, tier="change": frozenset({"deploy", "secrets"}))
    def test_a_light_pass_covers_formal_source_changes(self, changed_files):
        areas = plan.check_areas(world.areas(FORMAL_PROOF_FILES), FORMAL_PROOF_FILES, config.SKIP_AREAS)
        self.assertEqual(plan.group_of(areas), "light")
        with state.change() as s:
            s["batches"]["light-1791046317"] = {"areas": ["deploy", "secrets"], "started": 1}
            s["passed"]["921"] = {"sha": "s921", "main": "m", "order": [921], "batch": "light-1791046317"}
        pr = plan.Pr(921, "s921", areas, draft=True)
        with mock.patch.object(world, "merged_since", return_value=[]), \
                mock.patch.object(world, "merges_cleanly", return_value=[]), \
                mock.patch.object(world, "mark_ready") as mark_ready:
            self.assertEqual(tick.ready([pr], dry_run=False), [921])
            mark_ready.assert_called_once_with(921)
        self.assertFalse(plan.needs_check(pr, plan.Passed("s921", "m", [921], "light-1791046317"), None,
                                          ["deploy", "secrets"]))

    def test_a_machine_failure_blames_nobody(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(infrastructure=True))
        s = state.read()
        self.assertEqual((s["failed"], s["passed"]), ({}, {}))
        self.assertEqual(s["batches"]["b"]["status"], "infrastructure")

    def test_a_skipped_member_is_neither_passed_nor_failed(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(skip={2}))
        s = state.read()
        self.assertEqual((set(s["passed"]), s["failed"]), ({"1"}, {}))

    def test_two_broken_members_are_both_found(self):
        self.start([1, 2, 3, 4])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(broken={2, 4}))
        s = state.read()
        self.assertEqual(set(s["failed"]), {"2", "4"})
        self.assertEqual(s["batches"]["b"]["passed"], [1, 3])

    def test_a_batch_that_merged_nothing_passes_nobody(self):
        self.start([1, 2])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(skip={1, 2}))
        s = state.read()
        self.assertEqual((s["passed"], s["batches"]["b"]["status"]), ({}, "nothing-merged"))

    def test_a_check_that_exits_without_a_result_is_infrastructure(self):
        self.start([1])
        batch.run("b", areas_of=lambda shas: frozenset(),
                  check=lambda name, areas, members: {"rc": -9, "process_rc": -9, "merged": [],
                                                     "skipped": [], "line": "merge-check left no result", "log": ""})
        self.assertEqual(state.read()["batches"]["b"]["status"], "infrastructure")

    def test_a_check_ignores_a_stale_result_and_keeps_its_exit_code(self):
        run = os.path.join(self.home, "runs", "stale")
        os.makedirs(run)
        with open(os.path.join(run, "result.json"), "w") as result:
            result.write('{"rc": 0, "merged": [1], "skipped": [], "line": "old pass"}')
        with mock.patch.object(batch, "RUNS", os.path.join(self.home, "runs")), \
             mock.patch.object(batch, "CHECK", "merge-check"), \
             mock.patch.object(batch, "_free_port", return_value=1234), \
             mock.patch.object(batch.subprocess, "run", return_value=subprocess.CompletedProcess([], -9)):
            result = batch.check("stale", ["rust"], [(1, "head")])
        self.assertEqual(result["process_rc"], -9)
        self.assertEqual(result["merged"], [])
        self.assertTrue(batch._not_run(result))

    def test_a_probe_that_could_not_run_blames_nobody_and_not_main(self):
        self.start([1, 2, 3])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks(broken={3}, unrunnable_after=2))
        s = state.read()
        self.assertEqual((s["failed"], s["batches"]["b"]["status"]), ({}, "infrastructure"))

    def test_older_code_keeps_keys_it_does_not_know(self):
        with open(state.PATH, "w") as f:
            f.write('{"a_newer_key": [1]}')
        with state.change():
            pass
        self.assertEqual(state.read()["a_newer_key"], [1])

    def test_a_pass_clears_an_old_failure_for_the_same_pr(self):
        with state.change() as s:
            s["failed"]["1"] = {"sha": "old"}
        self.start([1])
        batch.run("b", areas_of=lambda shas: frozenset(), check=FakeChecks())
        self.assertNotIn("1", state.read()["failed"])


if __name__ == "__main__":
    unittest.main()
