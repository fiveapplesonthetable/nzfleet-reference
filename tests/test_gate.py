import argparse
import io
import unittest
from unittest import mock

from nzfleet import areas


def run_with(order, failing=(), recorded=None):
    def on_host(area, root, target, slot, out, name="", sccache="", job=None, areas=(), cpus=None, stop=None):
        order.append(area)
        return (1 if area in failing else 0), 0.0

    args = argparse.Namespace(workflow="w", worktree="/w", target="/t", main="m", cache="/c", name="n", slot="",
                              sccache="", sccache_bin="", stats="stats.json", areas=["rust", "console", "compile", "android"])
    workflow = {"jobs": {"android": {"env": {
        "JAVA_TOOL_OPTIONS": "${{ vars.CI_RUNS_ON && '-XX:ActiveProcessorCount=6' || '' }}",
        "GRADLE_OPTS": "${{ vars.CI_RUNS_ON && '-Dorg.gradle.workers.max=6' || '' }}",
    }}}}
    with mock.patch.object(areas.yaml, "safe_load", return_value=workflow), mock.patch("builtins.open", mock.mock_open()), \
            mock.patch.object(areas.subprocess, "run", return_value=mock.Mock(stdout="/g")), \
            mock.patch.object(areas.os, "makedirs"), mock.patch.object(areas, "run_on_host", on_host), \
            mock.patch.object(areas.json, "dump", side_effect=lambda data, stream: recorded.update(data) if recorded is not None else None), \
            mock.patch("sys.stdout", new=io.StringIO()):
        return areas.run(args)


class TheGateRunsFirst(unittest.TestCase):
    def test_it_starts_before_any_other_area(self):
        order = []
        self.assertEqual(run_with(order), 0)
        self.assertEqual(order[0], "compile")
        self.assertEqual(sorted(order), ["android", "compile", "console", "rust"])

    def test_a_failed_rust_area_fails_the_run(self):
        order = []
        recorded = {}
        self.assertEqual(run_with(order, failing={"rust"}, recorded=recorded), 1)
        self.assertEqual(recorded["rust"]["rc"], 1)

    def test_a_client_that_does_not_compile_stops_the_long_areas(self):
        order = []
        recorded = {}
        self.assertEqual(run_with(order, failing={"compile"}, recorded=recorded), 1)
        self.assertEqual(order, ["compile"])
        self.assertEqual(recorded, {"compile": mock.ANY})
        self.assertEqual(recorded["compile"]["rc"], 1)


class TheFirstRedAreaStopsTheOthers(unittest.TestCase):
    def run_areas(self, failing):
        import threading
        stop_seen, recorded = {}, {}
        failed_once, all_running = threading.Event(), threading.Barrier(3)

        def on_host(area, root, target, slot, out, name="", sccache="", job=None, areas=(), cpus=None, stop=None):
            killed = threading.Event()
            if area in failing:
                all_running.wait()
                return 1, 0.0
            stop.register(area, killed.set)
            all_running.wait()
            failed_once.wait()
            stop_seen[area] = killed.wait()
            return (1, 0.0) if killed.is_set() else (0, 0.0)

        args = argparse.Namespace(workflow="w", worktree="/w", target="/t", main="m", cache="/c", name="n", slot="",
                                  sccache="", sccache_bin="", stats="stats.json", areas=["rust", "console", "e2e"])
        real_trip = areas.Stop.trip
        with mock.patch.object(areas.Stop, "trip", lambda self, a: (real_trip(self, a), failed_once.set())), \
                mock.patch.object(areas.yaml, "safe_load", return_value={"jobs": {}}), mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(areas.subprocess, "run", return_value=mock.Mock(stdout="/g")), \
                mock.patch.object(areas.os, "makedirs"), mock.patch.object(areas, "run_on_host", on_host), \
                mock.patch.object(areas.json, "dump", side_effect=lambda data, stream: recorded.update(data)), \
                mock.patch("sys.stdout", new=io.StringIO()) as printed:
            code = areas.run(args)
        return code, recorded, stop_seen, printed.getvalue()

    def test_the_areas_still_running_are_killed_and_not_called_failed(self):
        code, recorded, seen, text = self.run_areas({"rust"})
        self.assertEqual((code, seen, text.splitlines()[-1]), (1, {"console": True, "e2e": True}, "failed: rust"))
        self.assertEqual({a: s["rc"] for a, s in recorded.items()}, {"rust": 1, "console": areas.STOPPED, "e2e": areas.STOPPED})


class AFinishedAreaIsNotSignalled(unittest.TestCase):
    def test_trip_leaves_alone_an_area_that_already_ended(self):
        stop, killed = areas.Stop(), []
        stop.register("rust", lambda: killed.append("rust"))
        stop.register("e2e", lambda: killed.append("e2e"))
        stop.finish("e2e")
        stop.trip("console")
        self.assertEqual(killed, ["rust"])
