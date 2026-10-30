import json
import os
import subprocess
import tempfile
import unittest

from nzfleet import shotsaccept

from test_shots import png

CONSOLE = "frontend/admin/screenshots/"
SIGNIN = CONSOLE + "01-signin.png"
HOME = CONSOLE + "02-home.png"
SENDERS = CONSOLE + "07-senders.png"
SCREEN = "frontend/admin/src/screens/Senders.tsx"
OLD = png(40, 40, lambda x, y: (10, 10, 10, 255))
NEW = png(40, 40, lambda x, y: (200, 10, 10, 255))


def write(root, path, data):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


class Accept(unittest.TestCase):
    """A checkout on a branch off `main` that changes Senders.tsx, and a run drawn from that very tree."""

    def setUp(self):
        self.checkout, self.run = tempfile.mkdtemp(), tempfile.mkdtemp()
        write(self.checkout, "frontend/admin/screenshots.golden", "01-signin.png\n02-home.png\n")
        for path in (SIGNIN, HOME, SENDERS):
            write(self.checkout, path, OLD)
        write(self.checkout, SCREEN, "one")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.commit("base")
        self.git("checkout", "-q", "-b", "change")
        write(self.checkout, SCREEN, "two")
        self.git("add", "-A")
        self.commit("change")
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        write(self.run, "result.json", json.dumps({"tree": self.tree}))
        for path in (SIGNIN, SENDERS):
            self.draw(path, OLD, NEW)
        self.draw(HOME, OLD, OLD)

    def git(self, *args):
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.checkout,
                              capture_output=True, text=True, check=True)
        return done.stdout

    def commit(self, message):
        self.git("commit", "-q", "-m", message)

    def draw(self, path, before, after):
        for side, data in (("before", before), ("after", after)):
            write(self.run, f"shots/console/{side}/{path}", data)
        with open(os.path.join(self.run, "shots/console/changed.txt"), "a") as f:
            f.write(f"changed\t{path}\t\n")

    def accept(self, everything=False, force=False):
        git = lambda *args: self.git(*args)
        return shotsaccept.accept(self.run, self.checkout, ["console", "android"], everything, force, git, base="main")

    def on_disk(self, path):
        with open(os.path.join(self.checkout, path), "rb") as f:
            return f.read()

    def test_only_the_goldens_that_moved_are_written(self):
        self.assertEqual(self.accept(), [SIGNIN])
        self.assertEqual(self.on_disk(SIGNIN), NEW)
        self.assertEqual(self.on_disk(HOME), OLD)
        self.assertEqual(self.on_disk(SENDERS), OLD)

    def test_all_writes_the_other_moved_images_too(self):
        self.assertEqual(self.accept(everything=True), [SIGNIN, SENDERS])
        self.assertEqual(self.on_disk(SENDERS), NEW)
        self.assertEqual(self.on_disk(HOME), OLD)

    def test_a_run_drawn_without_this_checkouts_change_is_refused(self):
        other = self.git("rev-parse", "main^{tree}").strip()
        write(self.run, "result.json", json.dumps({"tree": other}))
        with self.assertRaises(shotsaccept.Refused) as said:
            self.accept()
        self.assertIn("--force", str(said.exception))
        self.assertEqual(self.on_disk(SIGNIN), OLD)

    def test_force_copies_from_a_run_drawn_elsewhere(self):
        write(self.run, "result.json", json.dumps({"tree": self.git("rev-parse", "main^{tree}").strip()}))
        self.assertEqual(self.accept(force=True), [SIGNIN])

    def test_a_run_that_recorded_no_tree_is_refused(self):
        os.remove(os.path.join(self.run, "result.json"))
        with self.assertRaises(shotsaccept.Refused):
            self.accept()

    def test_a_batch_id_names_its_highest_round(self):
        root = tempfile.mkdtemp()
        for name in ("b-1-r0", "b-1-r2", "b-1-r10", "b-12-r0"):
            os.makedirs(os.path.join(root, "runs", name))
        old, shotsaccept.HOME = shotsaccept.HOME, root
        try:
            self.assertEqual(shotsaccept.run_dir("b-1"), os.path.join(root, "runs", "b-1-r10"))
            self.assertEqual(shotsaccept.run_dir(self.run), self.run)
        finally:
            shotsaccept.HOME = old


if __name__ == "__main__":
    unittest.main()
