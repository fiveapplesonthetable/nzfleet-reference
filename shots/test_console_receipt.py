"""The recorded request set bounds local case and image selection."""

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE = Path(__file__).with_name("console-receipt.py")
SPEC = importlib.util.spec_from_file_location("console_receipt", MODULE)
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


def evidence():
    leaf = "frontend/admin/src/screens/Reports.tsx"
    shell = "assets/shell-A.js"
    reports = "assets/reports-A.js"
    unrelated = "assets/dashboard-A.js"
    old = {
        "version": 1,
        "assets": [unrelated, reports, shell, "assets/global.css", "index.html"],
        "hashes": ["dashboard", "old-reports", "shell", "css", "html"],
        "chunks": {
            "dashboard-key": {"file": unrelated, "modules": ["src/screens/Home.tsx"], "imports": ["shell-key"]},
            "reports-key": {"file": reports, "modules": ["src/screens/Reports.tsx"], "imports": ["shell-key"]},
            "shell-key": {"file": shell, "modules": ["src/App.tsx"], "imports": []},
        },
        "asset_sets": [[], [0, 1, 2], [0, 2], [1, 2]],
        "cases": {"reports direct": 3, "dashboard button opens reports": 1,
                  "dashboard only": 2, "unobserved unit contract": 0},
        "shots": {"11-reports": 3, "11-reports-dark": 3,
                  "02-dashboard": 2, "02-dashboard-dark": 2,
                  "02-route-boundary": 0, "02-route-boundary-dark": 0},
    }
    new_chunks = {
        "assets/dashboard-A.js": {"modules": ["src/screens/Home.tsx"], "imports": ["assets/shell-A.js"]},
        "assets/reports-B.js": {"modules": ["src/screens/Reports.tsx"], "imports": ["assets/shell-A.js"]},
        "assets/shell-A.js": {"modules": ["src/App.tsx"], "imports": []},
    }
    keys = {"assets/dashboard-A.js": "dashboard-key", "assets/reports-B.js": "reports-key",
            "assets/shell-A.js": "shell-key"}
    assets = {"assets/dashboard-A.js": "dashboard", "assets/reports-B.js": "new-reports",
              "assets/shell-A.js": "shell", "assets/global.css": "css", "index.html": "html"}
    return old, [("M", leaf)], new_chunks, keys, assets


class ReceiptSelection(unittest.TestCase):
    def test_only_declared_chunk_references_are_normalized(self):
        old = b'import("./Reports-A.js");const label="Other-A.js"'
        new = b'import("./Reports-B.js");const label="Other-B.js"'
        old_keys = {"assets/Reports-A.js": "reports", "assets/Other-A.js": "other"}
        new_keys = {"assets/Reports-B.js": "reports", "assets/Other-B.js": "other"}
        old_value = receipt.canonical_asset_bytes(old, ["assets/Reports-A.js"], old_keys)
        new_value = receipt.canonical_asset_bytes(new, ["assets/Reports-B.js"], new_keys)
        self.assertNotEqual(old_value, new_value)
        self.assertIn(b'@reports@', old_value)

    def test_python_bytecode_is_not_a_source_input(self):
        self.assertTrue(receipt.generated_input("scripts/__pycache__/visual-plan.cpython-312.pyc"))
        self.assertFalse(receipt.generated_input("tools/nzfleet/nzfleet/selection/changed_tests.py"))

    def test_missing_and_version_mismatched_receipts_fall_back(self):
        for value in ({}, {"version": 2, "scope": "chromium built console and pinned light/dark captures"}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "version or scope"):
                receipt.valid_receipt_header(value)

    def test_new_removed_and_renamed_screens_fall_back(self):
        path = "frontend/admin/src/screens/Reports.tsx"
        for changes in ([('A', path)], [('D', path)], [('R', path)], [('M', "frontend/admin/src/lib/theme.ts")]):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "direct screen leaf"):
                receipt.valid_leaf_changes(changes)

    def test_changed_leaf_selects_direct_and_cross_route_consumers_in_both_themes(self):
        result = receipt.verify_bundle(*evidence())
        self.assertEqual(result["cases"], ["dashboard button opens reports", "reports direct", "unobserved unit contract"])
        self.assertEqual(result["images"], ["02-route-boundary", "02-route-boundary-dark", "11-reports", "11-reports-dark"])
        self.assertEqual(result["shots"], ["02-route-boundary", "11-reports"])

    def test_shared_compiled_chunk_change_requires_full_run(self):
        old, changes, chunks, keys, assets = evidence()
        assets["assets/shell-A.js"] = "different"
        with self.assertRaisesRegex(ValueError, "shared chunk"):
            receipt.verify_bundle(old, changes, chunks, keys, assets)

    def test_global_css_change_requires_full_run(self):
        old, changes, chunks, keys, assets = evidence()
        assets["assets/global.css"] = "different"
        with self.assertRaisesRegex(ValueError, "global asset"):
            receipt.verify_bundle(old, changes, chunks, keys, assets)

    def test_dynamic_import_change_requires_full_run(self):
        old, changes, chunks, keys, assets = evidence()
        chunks["assets/reports-B.js"]["imports"].append("assets/dashboard-A.js")
        with self.assertRaisesRegex(ValueError, "dependency graph"):
            receipt.verify_bundle(old, changes, chunks, keys, assets)

    def test_missing_or_added_bundle_requires_full_run(self):
        old, changes, chunks, keys, assets = evidence()
        del chunks["assets/reports-B.js"]
        with self.assertRaisesRegex(ValueError, "membership"):
            receipt.verify_bundle(old, changes, chunks, keys, assets)

    def test_unobserved_changed_leaf_cannot_produce_empty_green(self):
        old, changes, chunks, keys, assets = evidence()
        old["cases"] = {"dashboard only": 2}
        old["shots"] = {"02-dashboard": 2}
        with self.assertRaisesRegex(ValueError, "no observed consumer"):
            receipt.verify_bundle(old, changes, chunks, keys, assets)

    def test_new_screen_input_requires_full_run(self):
        old, _, chunks, keys, assets = evidence()
        with self.assertRaisesRegex(ValueError, "unique bundle"):
            receipt.verify_bundle(old, [("M", "frontend/admin/src/screens/New.tsx")], chunks, keys, assets)

    def test_unmapped_trace_asset_keeps_its_case_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.zip"
            data = {"snapshot": {"request": {"url": "http://127.0.0.1:43180/assets/other.js"}, "response": {"status": 200}}}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("1-trace.network", json.dumps(data) + "\n")
            self.assertEqual(receipt.trace_assets(path, {"assets/reports-A.js"}), [])

    def test_failed_trace_asset_keeps_its_case_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.zip"
            data = {"snapshot": {"request": {"url": "http://127.0.0.1:43180/assets/reports-A.js"}, "response": {"status": 404}}}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("1-trace.network", json.dumps(data) + "\n")
            self.assertEqual(receipt.trace_assets(path, {"assets/reports-A.js"}), [])

    def test_trace_without_network_keeps_its_case_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("test.trace", "")
            self.assertEqual(receipt.trace_assets(path, {"assets/reports-A.js"}), [])

    def test_missing_case_trace_cannot_be_recorded(self):
        report = {"suites": [{"title": "suite.spec.ts", "file": "suite.spec.ts", "specs": [{
            "title": "opens Reports", "line": 1, "column": 1,
            "tests": [{"projectName": "chromium", "status": "expected", "results": [{"status": "passed", "retry": 0, "attachments": []}]}],
        }]}], "errors": []}
        with self.assertRaisesRegex(ValueError, "no trace"):
            receipt.report_cases(report, {"assets/reports-A.js"})

    def test_failed_case_cannot_be_recorded(self):
        report = {"suites": [{"title": "suite.spec.ts", "file": "suite.spec.ts", "specs": [{
            "title": "opens Reports", "line": 1, "column": 1,
            "tests": [{"projectName": "chromium", "status": "unexpected", "results": [{"status": "failed", "retry": 0}]}],
        }]}], "errors": []}
        with self.assertRaisesRegex(ValueError, "skipped"):
            receipt.report_cases(report, {"assets/reports-A.js"})

    def test_grouped_case_identity_matches_playwright_test_list(self):
        report = {"suites": [{"title": "sitemap.spec.ts", "file": "sitemap.spec.ts", "suites": [{
            "title": "the sidebar", "file": "sitemap.spec.ts", "specs": [{
                "title": "opens Reports", "line": 128, "column": 3, "tests": [{"projectName": "chromium"}],
            }],
        }]}]}
        self.assertEqual(receipt.expected_cases(report), {
            "[chromium] › sitemap.spec.ts:128:3 › the sidebar › opens Reports",
        })


if __name__ == "__main__":
    unittest.main()
