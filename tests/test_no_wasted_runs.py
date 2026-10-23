import os
import tempfile
import unittest
from unittest import mock

from nzfleet import batch, plan, refresh, state, tick, world
from tests.fakeworld import CODE, Runner, World

APP = "frontend/android/app/src/main/java/app/nzela/ui/amount/AmountContent.kt"
TOOL = "tools/nzfleet/nzfleet/plan.py"
SHOT = "frontend/admin/screenshots/07-senders.png"
ALL = frozenset({"secrets", "rust", "fuzz", "e2e", "console", "android", "deploy"})


class Recording(Runner):
    """A runner that remembers the areas of each run and can hand back redrawn shots."""

    def __init__(self, shots=None):
        super().__init__()
        self.areas, self.shots = [], shots

    def __call__(self, name, areas, members):
        self.areas.append((name, sorted(areas), [n for n, _ in members]))
        result = super().__call__(name, areas, members)
        return {**result, "shots": self.shots} if self.shots else result


def redrawn_shots(root, path):
    """A run directory holding one changed console shot whose bytes differ."""
    for side, body in (("before", b"a"), ("after", b"b")):
        target = os.path.join(root, "console", side, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(body)
    with open(os.path.join(root, "console", "changed.txt"), "w") as f:
        f.write(f"changed\t{path}\t\n")
    return root


class WhatAChangeReads(unittest.TestCase):
    def areas(self, *files):
        return plan.check_areas(ALL, list(files), frozenset())

    def test_a_tools_only_change_draws_no_screenshot(self):
        self.assertFalse({"console", "android"} & self.areas(TOOL))

    def test_an_app_only_change_does_not_fuzz(self):
        self.assertNotIn("fuzz", self.areas(APP))

    def test_a_change_to_the_service_still_reaches_both_frontends_and_the_fuzzers(self):
        self.assertTrue({"console", "android", "fuzz"} <= self.areas(CODE))

    def test_a_console_change_leaves_android_alone(self):
        got = self.areas("frontend/admin/src/screens/Senders.tsx")
        self.assertEqual(("console" in got, "android" in got), (True, False))


class ATrainAlreadyVerified(unittest.TestCase):
    def test_the_same_heads_on_the_same_main_reuse_the_verdict(self):
        # Both ticks in one second, where a batch id made of the second alone overwrote the first verdict.
        with World() as w, mock.patch.object(tick.time, "time", return_value=1_791_674_495.0):
            w.open_pr(1, {CODE: "one\n"})
            w.runner = Recording()
            w.tick()
            first = len(w.runner.areas)
            with state.change() as s:
                s["passed"].clear()
            w.tick()
        # The first tick runs rust and the compile gate; the second runs nothing.
        self.assertEqual((first > 0, len(w.runner.areas)), (True, first))

    def test_a_moved_main_runs_the_train_again(self):
        with World() as w:
            w.open_pr(1, {CODE: "one\n"})
            w.runner = Recording()
            w.tick()
            w.advance_main({"docs/a.md": "x\n"})
            with state.change() as s:
                s["passed"].clear()
            w.tick()
        self.assertEqual(len(w.runner.areas), 2)


class ShotsNoPrOwns(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.shots = redrawn_shots(self.dir, SHOT)
        patches = [mock.patch.object(refresh, "golden", return_value=set()),
                   mock.patch.object(refresh, "open_main_pr", return_value=77)]
        self.opened = patches[1].start()
        patches[0].start()
        self.addCleanup(mock.patch.stopall)

    def train(self, w, now=None):
        w.open_pr(1, {"docs/a.md": "x\n"})
        w.runner = Recording(self.shots)
        with mock.patch.object(world, "changed_files", return_value=["docs/a.md"]):
            w.tick()

    def test_main_is_drawn_alone_and_one_refresh_is_opened(self):
        with World() as w:
            self.train(w)
            names = [name.rsplit("-", 1)[-1] for name, _, members in w.runner.areas if not members]
            recorded = state.read()["stale_shots"]["pr"]
        self.assertEqual((names, self.opened.call_count, recorded), (["main"], 1, 77))

    def test_a_second_batch_on_the_same_main_does_not_draw_it_again(self):
        with World() as w:
            self.train(w)
            w.open_pr(2, {"docs/b.md": "y\n"})
            with mock.patch.object(world, "changed_files", return_value=["docs/b.md"]):
                w.tick()
            alone = [1 for _, _, members in w.runner.areas if not members]
        self.assertEqual((len(alone), self.opened.call_count), (1, 1))

    def test_a_refresh_opened_today_is_not_followed_by_another_on_a_newer_main(self):
        record = {"main": "old", "opened": 1000.0, "pr": 5}
        self.assertEqual(plan.stale_shots_step(True, "new", record, 1000.0 + 3600), "wait")
        self.assertEqual(plan.stale_shots_step(True, "new", record, 1000.0 + plan.DAY), "redraw")

    def test_shots_a_member_owns_do_not_draw_main(self):
        with World() as w:
            w.open_pr(1, {"frontend/admin/src/screens/Senders.tsx": "x\n"})
            w.runner = Recording(self.shots)
            with mock.patch.object(world, "changed_files", return_value=["frontend/admin/src/screens/Senders.tsx"]), \
                    mock.patch.object(refresh, "commit_train", return_value=""):
                w.tick()
        self.assertEqual([1 for _, _, members in w.runner.areas if not members], [])
        self.assertEqual(self.opened.call_count, 0)


class ATaskThatUnblocksMerges(unittest.TestCase):
    TODO = [{"key": "a", "task": "", "area": "x"}, {"key": "b", "task": "", "area": "x"},
            {"key": "urgent", "task": "", "area": "x", "now": True}]

    def test_it_starts_ahead_of_the_queue(self):
        self.assertEqual(plan.next_work(self.TODO, {}, 0, 4, False)["key"], "urgent")

    def test_it_ignores_its_areas_lane(self):
        agents = {"a": {"status": "running"}}
        self.assertEqual(plan.next_work(self.TODO, agents, 1, 4, False)["key"], "urgent")

    def test_it_takes_a_slot_even_when_all_are_busy(self):
        self.assertEqual(plan.next_work(self.TODO, {}, 4, 4, False)["key"], "urgent")

    def test_the_queue_behind_it_keeps_its_own_lane(self):
        agents = {"urgent": {"status": "running"}}
        self.assertEqual(plan.next_work(self.TODO, agents, 1, 4, False)["key"], "a")

    def test_an_unmet_prerequisite_still_holds_it_back(self):
        todo = [{"key": "urgent", "task": "", "now": True, "after": ["pr:9"]}]
        self.assertIsNone(plan.next_work(todo, {}, 0, 4, False, {9: "waiting"}))


class ABatchIdIsNeverReused(unittest.TestCase):
    def test_two_batches_started_in_one_second_get_different_ids(self):
        with mock.patch("time.time", return_value=1000.0):
            first = tick._batch_id("core", {})
            second = tick._batch_id("core", {first: {}})
        self.assertNotEqual(first, second)
