import unittest
from unittest import mock

from nzfleet import land, state, world

from fakeworld import World


def file(n):
    return f"crates/nzela/src/pr{n}.rs"


def train_of(w):
    """Opens PRs 1 and 2 (2 stacked on 1), ticks until they are a train, and returns it."""
    w.open_pr(1, {file(1): "one\n"})
    w.open_pr(2, {file(2): "two\n"}, base="pr1")
    train, = w.tick()["trains"].values()
    return train


def status_of(train):
    return state.read()["trains"][train["id"]]["status"]


class Landing(unittest.TestCase):
    def test_a_train_is_refused_once_main_has_moved(self):
        with World() as w:
            train = train_of(w)
            w.advance_main({"README.md": "elsewhere\n"})
            merged = []
            with mock.patch.object(world, "merge_pr", side_effect=merged.append):
                self.assertEqual(land.land(train["id"]), 1)
            self.assertEqual(merged, [])

    def test_the_prs_are_merged_in_the_order_they_were_verified(self):
        with World() as w:
            train = train_of(w)
            order = []
            with mock.patch.object(world, "merge_pr", side_effect=lambda n: (order.append(n), w.merge_pr(n))):
                self.assertEqual(land.land(train["id"]), 0)
            self.assertEqual(order, [1, 2])
            self.assertEqual(status_of(train), "landed")

    def test_main_ends_as_the_tree_that_was_verified(self):
        with World() as w:
            train = train_of(w)
            land.land(train["id"])
            self.assertEqual(world.main_tree(), train["tree"])

    def test_a_tree_that_is_not_the_verified_one_is_posted_loudly(self):
        with World() as w:
            train = train_of(w)

            def merge_and_sneak_in_a_change(n):
                w.merge_pr(n)
                if n == 2:
                    w.advance_main({"README.md": "not verified\n"})

            with mock.patch.object(world, "merge_pr", side_effect=merge_and_sneak_in_a_change):
                self.assertEqual(land.land(train["id"]), 1)
            self.assertEqual(status_of(train), "mismatch")
            self.assertIn("not the verified", w.comments[-1])
            self.assertTrue(w.mailed("did not land as verified"))

    def test_a_merge_outside_a_train_invalidates_the_open_train(self):
        with World() as w:
            train = train_of(w)
            w.advance_main({"README.md": "elsewhere\n"})
            w.tick()
            self.assertEqual(status_of(train), "invalidated")
            self.assertEqual(land.land(train["id"]), 1)

    def test_landing_a_train_does_not_invalidate_it_on_the_next_tick(self):
        with World() as w:
            train = train_of(w)
            land.land(train["id"])
            w.tick()
            self.assertEqual(status_of(train), "landed")

    def test_a_head_that_moved_since_it_was_verified_stops_the_landing(self):
        with World() as w:
            train = train_of(w)
            w.open_pr(2, {file(2): "changed\n"}, base="pr1")
            self.assertEqual(land.land(train["id"]), 1)
            self.assertEqual(world.main_sha(), train["base"])

    def test_the_mergeability_recompute_is_retried_without_sleeping(self):
        with World() as w:
            train = train_of(w)
            attempts = []

            def refuse_twice(n):
                attempts.append(n)
                if len(attempts) <= 2:
                    raise world.Failed("Pull request is not mergeable")
                w.merge_pr(n)

            with mock.patch.object(world, "merge_pr", side_effect=refuse_twice):
                self.assertEqual(land.land(train["id"]), 0)
            self.assertEqual(attempts[:3], [1, 1, 1])
            self.assertEqual(len(w.pauses), 2)

    def test_a_pr_that_never_merges_stops_the_train_and_says_so(self):
        with World() as w:
            train = train_of(w)

            def second_never_merges(n):
                if n == 2:
                    raise world.Failed("not mergeable")
                w.merge_pr(n)

            with mock.patch.object(world, "merge_pr", side_effect=second_never_merges):
                self.assertEqual(land.land(train["id"]), 1)
            self.assertEqual(status_of(train), "stopped")
            self.assertIn("stopped at #2 after merging #1", w.comments[-1])
