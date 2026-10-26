"""A changed or absent planned image needs review."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("visual_diff", Path(__file__).with_name("visual-diff.py"))
visual_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual_diff)


class VisualDiffTest(unittest.TestCase):
    def test_changed_and_missing_images_cannot_pass(self):
        old = {"sha256": "old", "dimensions": [780, 1688]}
        new = {"sha256": "new", "dimensions": [780, 1688]}
        self.assertEqual(visual_diff.image_status(old, new), "changed")
        self.assertEqual(visual_diff.image_status(None, None), "missing")
        self.assertEqual(visual_diff.image_status(None, new), "new")
        self.assertEqual(visual_diff.image_status(old, None), "removed")
        self.assertEqual(visual_diff.image_status(new, new), "same")

    def test_a_partial_console_shot_checks_the_dark_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path("frontend/admin/screenshots/11-reports.png")
            for side in ("before", "after"):
                directory = root / side / "frontend/admin/screenshots"
                directory.mkdir(parents=True)
                shutil.copyfile(source, directory / "11-reports.png")
                shutil.copyfile(source, directory / "11-reports-dark.png")
            plan = {"version": 1, "selected": {"console": ["11-reports", "11-reports-dark"], "android": []},
                    "deleted": {"console": [], "android": []}, "full_fallback": {"console": False, "android": False}}
            plan_file = root / "plan.json"
            plan_file.write_text(json.dumps(plan))
            command = [sys.executable, str(Path(__file__).with_name("visual-diff.py")), "--plan", str(plan_file),
                       "--before", str(root / "before"), "--after", str(root / "after"), "--out", str(root / "diff")]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["images"], 2)
            shutil.copyfile(Path("frontend/admin/screenshots/11-reports-period-dark.png"), root / "after/frontend/admin/screenshots/11-reports-dark.png")
            command[-1] = str(root / "changed")
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            rows = json.loads((root / "changed/result.json").read_text())
            self.assertEqual([row["stem"] for row in rows if row["status"] == "changed"], ["11-reports-dark"])
            (root / "after/frontend/admin/screenshots/11-reports-dark.png").unlink()
            command[-1] = str(root / "missing")
            third = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(third.returncode, 0)
            rows = json.loads((root / "missing/result.json").read_text())
            self.assertEqual([row["stem"] for row in rows if row["status"] == "removed"], ["11-reports-dark"])
            (root / "before/frontend/admin/screenshots/11-reports-dark.png").unlink()
            command[-1] = str(root / "absent")
            fourth = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(fourth.returncode, 0)
            rows = json.loads((root / "absent/result.json").read_text())
            self.assertEqual([row["stem"] for row in rows if row["status"] == "missing"], ["11-reports-dark"])
            shutil.copyfile(source, root / "after/frontend/admin/screenshots/11-reports-dark.png")
            command[-1] = str(root / "added")
            fifth = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(fifth.returncode, 0)
            rows = json.loads((root / "added/result.json").read_text())
            self.assertEqual([row["stem"] for row in rows if row["status"] == "new"], ["11-reports-dark"])


if __name__ == "__main__":
    unittest.main()
