import os
import subprocess
import tempfile
import unittest

from nzfleet import plan, shape, world

SHOT = "frontend/admin/screenshots/a.png"


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Files(unittest.TestCase):
    def test_screenshots_alone_are_redrawn_by_their_area(self):
        self.assertEqual(plan.generated_areas([SHOT, "frontend/android/docs/screenshots/b.png"]),
                         frozenset({"console", "android"}))

    def test_one_hand_written_file_needs_a_person(self):
        self.assertIsNone(plan.generated_areas([SHOT, "frontend/admin/src/App.tsx"]))

    def test_the_comparison_page_is_drawn(self):
        self.assertEqual(plan.generated_areas(["frontend/admin/docs/comparison.html"]), frozenset({"console"}))


class Repo(unittest.TestCase):
    """A main and two branches that each changed the same screenshot and their own code."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.old = world.REPO
        world.REPO = self.repo
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        self.write("code.txt", "base\n")
        self.write(SHOT, "base\n")
        self.commit("base")
        self.base = sh(self.repo, "git", "rev-parse", "HEAD")

    def tearDown(self):
        world.REPO = self.old

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self, message):
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", message)
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def branch(self, name, code_file, shot):
        sh(self.repo, "git", "checkout", "-q", "-b", name, self.base)
        self.write(code_file, name + "\n")
        self.write(SHOT, shot + "\n")
        return self.commit(name)

    def test_a_screenshot_conflict_takes_the_base(self):
        first = self.branch("first", "first.txt", "first")
        second = self.branch("second", "second.txt", "second")
        wt = tempfile.mkdtemp()
        sh(self.repo, "git", "worktree", "add", "-q", "--detach", wt, first)
        self.assertEqual(shape._apply(wt, second), [])
        self.assertEqual(open(os.path.join(wt, SHOT)).read(), "first\n")
        self.assertTrue(os.path.exists(os.path.join(wt, "second.txt")))

    def test_a_code_conflict_is_left_for_a_person(self):
        first = self.branch("first", "code.txt", "first")
        second = self.branch("second", "code.txt", "second")
        wt = tempfile.mkdtemp()
        sh(self.repo, "git", "worktree", "add", "-q", "--detach", wt, first)
        self.assertIn("code.txt", shape._apply(wt, second))

    def test_a_branch_replayed_with_new_screenshots_is_the_same_change(self):
        first = self.branch("first", "first.txt", "first")
        second = self.branch("second", "second.txt", "second")
        sh(self.repo, "git", "checkout", "-q", "-b", "replayed", first)
        self.write("second.txt", "second\n")
        self.write(SHOT, "redrawn\n")
        replayed = self.commit("second, replayed")
        self.assertEqual(world.change_id(replayed, first), world.change_id(second, self.base))

    def test_a_replay_that_changed_code_is_a_new_change(self):
        first = self.branch("first", "first.txt", "first")
        second = self.branch("second", "second.txt", "second")
        sh(self.repo, "git", "checkout", "-q", "-b", "replayed", first)
        self.write("second.txt", "second, edited\n")
        replayed = self.commit("second, edited")
        self.assertNotEqual(world.change_id(replayed, first), world.change_id(second, self.base))


if __name__ == "__main__":
    unittest.main()
