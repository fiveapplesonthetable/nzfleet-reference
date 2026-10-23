import unittest

from fakeworld import World, Runner

GREEN = ("completed", {"rust": "success"}, 70)
RED = ("completed", {"rust": "failure"}, 71)


def other(n):
    return f"crates/nzela/src/pr{n}.rs"


def broken_main(name, areas, members):
    return Runner.result(1, [n for n, _ in members], "test money::tests::adds ... FAILED")


def main_broken_batch(w):
    w.runner = broken_main
    w.open_pr(1, {other(1): "one\n"})
    w.open_pr(2, {other(2): "two\n"})
    s = w.tick()
    (bid, batch), = s["batches"].items()
    assert batch["status"] == "main-broken", batch["status"]
    return bid


class AMainBrokenBatch(unittest.TestCase):
    def test_it_is_re_batched_on_the_new_main_once_main_is_green_and_the_merged_member_is_forgotten(self):
        with World() as w:
            main_broken_batch(w)
            w.merge_pr(2)
            w.advance_main({"crates/nzela/src/fix.rs": "fix\n"})
            w.ci, w.runner = GREEN, Runner()
            w.tick()
            s = w.tick()
            (batch,) = s["batches"].values()
            self.assertEqual(([n for n, _ in batch["members"]], batch["status"]), ([1], "green"))
            self.assertEqual(batch["main"], w.main_sha())
            self.assertEqual(s["untied"], {})

    def test_it_stays_while_main_is_still_red(self):
        with World() as w:
            old = main_broken_batch(w)
            w.advance_main({"crates/nzela/src/fix.rs": "fix\n"})
            w.ci = RED
            w.tick()
            self.assertEqual(w.tick()["batches"][old]["status"], "main-broken")

    def test_it_stays_while_the_green_run_is_not_on_a_newer_commit(self):
        with World() as w:
            old = main_broken_batch(w)
            w.ci = GREEN
            w.tick()
            self.assertEqual(w.tick()["batches"][old]["status"], "main-broken")

    def test_it_is_dropped_when_every_member_is_merged_or_closed(self):
        with World() as w:
            old = main_broken_batch(w)
            w.merge_pr(1)
            w.merge_pr(2)
            self.assertNotIn(old, w.tick()["batches"])
