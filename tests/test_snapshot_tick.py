import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from nzfleet import snapshot_tick


class AtomicWrites(unittest.TestCase):
    def test_concurrent_cache_writers_keep_valid_json_and_only_their_temps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cost-cache.json")
            barrier = threading.Barrier(2)
            replace = os.replace

            def synchronized_replace(source, destination):
                barrier.wait(timeout=5)
                replace(source, destination)

            with mock.patch.object(snapshot_tick.os, "replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    results = list(workers.map(lambda data: snapshot_tick._write(path, data),
                                               ({"writer": "daemon"}, {"writer": "manual"})))

            self.assertEqual(results, [None, None])
            with open(path) as cache:
                self.assertIn(json.load(cache), ({"writer": "daemon"}, {"writer": "manual"}))
            self.assertEqual(os.listdir(directory), ["cost-cache.json"])

    def test_concurrent_page_writers_replace_complete_html_and_only_their_temps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "index.html")
            barrier = threading.Barrier(2)
            replace = os.replace

            def render(shot):
                barrier.wait(timeout=5)
                return f"<html>{shot['writer']}</html>"

            def synchronized_replace(source, destination):
                barrier.wait(timeout=5)
                replace(source, destination)

            with mock.patch.object(snapshot_tick.serve, "PAGE", path), \
                    mock.patch.object(snapshot_tick.page, "html", side_effect=render), \
                    mock.patch.object(snapshot_tick.os, "replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    results = list(workers.map(snapshot_tick._write_page,
                                               ({"writer": "daemon"}, {"writer": "manual"})))
            self.assertEqual(results, [None, None])
            with open(path) as page:
                self.assertIn(page.read(), ("<html>daemon</html>", "<html>manual</html>"))
            self.assertEqual(os.listdir(directory), ["index.html"])

    def test_serialization_failure_leaves_the_destination_and_cleans_its_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            with open(path, "w") as target:
                target.write("old")
            with mock.patch.object(snapshot_tick.json, "dump", side_effect=ValueError("bad value")):
                with self.assertRaisesRegex(ValueError, "bad value"):
                    snapshot_tick._write(path, {})
            with open(path) as target:
                self.assertEqual(target.read(), "old")
            self.assertEqual(os.listdir(directory), ["cache.json"])

    def test_replace_permission_failure_surfaces_and_cleans_its_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "index.html")
            with open(path, "w") as target:
                target.write("old")
            with mock.patch.object(snapshot_tick.os, "replace", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(PermissionError, "denied"):
                    snapshot_tick._write_text(path, "new")
            with open(path) as target:
                self.assertEqual(target.read(), "old")
            self.assertEqual(os.listdir(directory), ["index.html"])

    def test_html_render_failure_surfaces_and_cleans_its_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "index.html")
            with mock.patch.object(snapshot_tick.serve, "PAGE", path), \
                    mock.patch.object(snapshot_tick.page, "html", side_effect=ValueError("render failed")):
                with self.assertRaisesRegex(ValueError, "render failed"):
                    snapshot_tick._write_page({})
            self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
    unittest.main()
