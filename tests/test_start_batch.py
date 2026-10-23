import os
import tempfile
import unittest
from unittest import mock

from nzfleet import batch, cli, state, supervise, world


class OneBatchPerSet(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.spawned, self.fetches = [], []
        self.fail_fetches = 0
        for patch in (mock.patch.object(state, "HOME", home.name), mock.patch.object(state, "PATH", os.path.join(home.name, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(home.name, "state.lock")),
                      mock.patch.object(batch, "_spawn", side_effect=self.spawn), mock.patch.object(world, "fetch", side_effect=self.fetch),
                      mock.patch.object(world, "head_of", side_effect=lambda n: f"sha{n}"), mock.patch.object(world, "main_sha", return_value="main"),
                      mock.patch.object(batch, "areas_of", return_value=frozenset({"rust"}))):
            patch.start()
            self.addCleanup(patch.stop)

    def spawn(self, bid):
        self.spawned.append(bid)
        return os.getpid()

    def fetch(self, prs):
        self.fetches.append(list(prs))
        if self.fail_fetches:
            self.fail_fetches -= 1
            raise world.Failed("couldn't find remote ref pull/9/head")

    def records(self):
        return state.read()["batches"]

    def test_two_starts_for_the_same_prs_make_one_batch(self):
        first, made = batch.start_batch([1, 2])
        again, made_again = batch.start_batch([2, 1])
        self.assertEqual((first, made, again, made_again), (first, True, first, False))
        self.assertEqual((len(self.records()), self.spawned), (1, [first]))

    def test_a_train_followed_by_a_manual_batch_of_it_makes_one_batch(self):
        batch.start_batch([9])
        cli.manual_batch(mock.Mock(foreground=False, prs=[9], areas=None))
        self.assertEqual(len(self.records()), 1)

    def test_a_foreground_batch_over_a_running_set_does_not_start_another(self):
        batch.start_batch([9])
        with mock.patch.object(batch, "run") as run:
            code = cli.manual_batch(mock.Mock(foreground=True, prs=[9], areas=None))
        run.assert_not_called()
        self.assertEqual((code, len(self.records())), (0, 1))

    def test_different_sets_each_get_a_batch(self):
        batch.start_batch([1])
        batch.start_batch([1, 2])
        self.assertEqual(len(self.records()), 2)

    def test_a_batch_whose_process_is_gone_does_not_block_a_new_one(self):
        old, _ = batch.start_batch([1])
        with state.change() as s:
            s["batches"][old]["pid"] = 2**22 + 1
        new, made = batch.start_batch([1])
        self.assertEqual((made, new != old), (True, True))

    def test_a_finished_batch_does_not_block_a_new_one(self):
        old, _ = batch.start_batch([1])
        with state.change() as s:
            s["batches"][old]["status"] = "green"
        self.assertTrue(batch.start_batch([1])[1])

    def test_a_record_written_before_start_batch_existed_is_found(self):
        with state.change() as s:
            s["batches"]["manual-5"] = {"group": "manual", "members": [[7, "s"]], "areas": ["rust"], "main": "m",
                                        "started": 1.0, "status": "running", "pid": os.getpid()}
        self.assertEqual(batch.start_batch([7]), ("manual-5", False))

    def test_the_record_holds_the_spawned_pid_and_the_heads_fetched(self):
        bid, _ = batch.start_batch([3])
        record = self.records()[bid]
        self.assertEqual((record["members"], record["areas"], record["main"], record["status"]), ([[3, "sha3"]], ["rust"], "main", "running"))

    def test_starting_a_batch_stops_registered_starve_burners(self):
        stopped = []
        with state.change() as data:
            data["starves"]["123"] = {"pids": [321, 654]}
        with mock.patch.object(batch.starve, "stop_burner", side_effect=stopped.append):
            batch.start_batch([8])
        self.assertEqual(stopped, [321, 654])

    def test_a_pull_ref_that_is_not_there_yet_is_fetched_again(self):
        self.fail_fetches = 2
        sleeps = []
        bid, _ = batch.start_batch([9], sleep=sleeps.append)
        self.assertEqual((len(self.fetches), len(sleeps), self.records()[bid]["members"]), (3, 2, [[9, "sha9"]]))

    def test_a_pull_ref_that_never_appears_fails_without_a_record(self):
        self.fail_fetches = 99
        with self.assertRaises(world.Failed):
            batch.start_batch([9], sleep=lambda s: None)
        self.assertEqual((len(self.fetches), self.records(), self.spawned), (batch.FETCH_TRIES, {}, []))

    def test_a_batch_that_cannot_be_spawned_is_lost_and_the_start_fails(self):
        with mock.patch.object(batch, "_spawn", side_effect=OSError("no systemd-run")), self.assertRaises(OSError):
            batch.start_batch([4])
        self.assertEqual([b["status"] for b in self.records().values()], ["lost"])

    def test_heads_the_caller_holds_are_not_fetched(self):
        batch.start_batch([5], ["rust"], "core", {5: "held"})
        self.assertEqual((self.fetches, list(self.records().values())[0]["members"]), ([], [[5, "held"]]))


class SupervisorKeepsTheOlder(unittest.TestCase):
    def test_the_newer_of_two_running_batches_over_one_set_is_the_one_stopped(self):
        run = lambda started, pid: {"status": "running", "members": [[1, "s"]], "started": started, "pid": pid, "group": "core"}
        self.assertEqual(supervise.duplicate_batches({"a": run(100, 11), "b": run(200, 12)}, {11, 12}), [("b", 12)])


if __name__ == "__main__":
    unittest.main()
