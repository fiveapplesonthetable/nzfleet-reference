import argparse
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from nzfleet import cli, plan, state, tick, world


class AnAttachedPullRequest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.enterContext(mock.patch.object(state, "PATH", os.path.join(home.name, "state.json")))
        self.enterContext(mock.patch.object(state, "LOCK", os.path.join(home.name, "state.lock")))
        with state.change() as data:
            data["todo"] = [{"key": "flaky", "area": "android"}, {"key": "next", "area": "android"}]
            data["agents"] = {"flaky": {"status": "done", "area": None}}

    def attach(self, number: int) -> None:
        with mock.patch.object(world, "branch_of", return_value="its-branch"), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.attach(argparse.Namespace(key="flaky", pr=str(number)))

    def test_once_merged_lets_the_next_item_in_its_area_start_on_main(self):
        self.attach(985)
        s = state.read()
        with mock.patch.object(world, "fetch"), \
                mock.patch.object(world, "head_of", return_value="merged-head"), \
                mock.patch.object(world, "is_ancestor", return_value=True), \
                mock.patch.object(world, "main_sha", return_value="main-head"):
            standing = tick.standings(s)
        verdict = plan.area_gate(s["todo"][1], s["todo"], s["agents"], standing)
        self.assertEqual(verdict, {"go": True, "base": {"on": "main"}})

    def test_takes_the_area_of_the_task_it_is_attached_to(self):
        self.attach(985)
        self.assertEqual(state.read()["agents"]["flaky"]["area"], "android")

    def test_recorded_without_an_area_is_still_read_for_its_area(self):
        with state.change() as data:
            data["agents"]["flaky"]["pr"] = {"number": 985, "branch": "b", "how": "attached"}
        with mock.patch.object(world, "fetch"), \
                mock.patch.object(world, "head_of", return_value="merged-head"), \
                mock.patch.object(world, "is_ancestor", return_value=True), \
                mock.patch.object(world, "main_sha", return_value="main-head"):
            self.assertEqual(tick.standings(state.read()), {985: "merged"})


if __name__ == "__main__":
    unittest.main()
