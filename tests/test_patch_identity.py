import os
import subprocess
import tempfile
import unittest
from unittest import mock

from nzfleet import world


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout.decode().strip()


class InvalidUtf8History(unittest.TestCase):
    """A repository whose history holds a file that is not UTF-8, as a Latin-1 document in the tree can."""

    def setUp(self):
        world._main_patch_ids.cache_clear()
        self.repo = tempfile.mkdtemp()
        self.old = (world.REPO, world.MAIN)
        world.REPO, world.MAIN = self.repo, "main"
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "config", "user.name", "t")
        self.write("base.txt", b"base\n")
        self.commit("base")

    def tearDown(self):
        world.REPO, world.MAIN = self.old
        world._main_patch_ids.cache_clear()

    def write(self, path, data):
        with open(os.path.join(self.repo, path), "wb") as f:
            f.write(data)

    def commit(self, message):
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", message)
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def branch_with(self, name, data):
        sh(self.repo, "git", "checkout", "-q", "-b", name, "main")
        self.write("latin1.txt", data)
        return self.commit(name)

    def land(self, branch):
        """Puts a branch's change on main as a commit of its own, as a squash merge does."""
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "merge", "-q", "--squash", branch)
        self.commit("land " + branch)

    def test_a_change_on_main_is_found_when_main_history_is_not_utf8(self):
        change = self.branch_with("change", b"caf\xa1\n")
        self.land("change")
        self.assertTrue(world.change_on_main(change))

    def test_a_change_not_on_main_is_not_found_when_main_history_is_not_utf8(self):
        self.branch_with("merged", b"caf\xa1\n")
        self.land("merged")
        other = self.branch_with("other", b"tea\n")
        self.assertFalse(world.change_on_main(other))

    def test_changes_differing_only_in_an_undecodable_byte_are_different_changes(self):
        first = self.branch_with("first", b"caf\xa1\n")
        second = self.branch_with("second", b"caf\xa2\n")
        self.assertNotEqual(world.change_id(first, "main"), world.change_id(second, "main"))

    def test_a_change_with_an_undecodable_byte_has_an_identity(self):
        change = self.branch_with("change", b"caf\xa1\n")
        self.assertRegex(world.change_id(change, "main"), r"^[0-9a-f]{40}$")

    def test_main_history_is_read_once_for_two_lookups_on_the_same_pinned_head(self):
        landed = self.branch_with("landed", b"landed\n")
        self.land("landed")
        missing = self.branch_with("missing", b"missing\n")
        main_head = world.main_sha()
        with mock.patch.object(world, "git_bytes", wraps=world.git_bytes) as git_bytes:
            self.assertTrue(world.change_on_main(landed, main_head=main_head))
            self.assertFalse(world.change_on_main(missing, main_head=main_head))
        history_reads = [call for call in git_bytes.call_args_list if call.args[:2] == ("log", "-p")]
        self.assertEqual(len(history_reads), 1)

    def test_a_new_main_head_gets_a_distinct_history_snapshot(self):
        landed = self.branch_with("landed", b"landed\n")
        pinned = world.main_sha()
        self.land("landed")
        current = world.main_sha()
        self.assertFalse(world.change_on_main(landed, main_head=pinned))
        self.assertTrue(world.change_on_main(landed, main_head=current))


if __name__ == "__main__":
    unittest.main()
