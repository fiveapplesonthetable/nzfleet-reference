"""Main is read from `upstream` on a fork checkout and from `origin` where that is the only remote, as in ci."""

import subprocess
import tempfile
import unittest
from unittest import mock

from nzfleet import world


def repo_with(*remotes: str) -> tempfile.TemporaryDirectory:
    repo = tempfile.TemporaryDirectory()
    subprocess.run(["git", "init", "-q", repo.name], check=True)
    for remote in remotes:
        subprocess.run(["git", "-C", repo.name, "remote", "add", remote, "https://example.invalid/r.git"], check=True)
    return repo


class MainIsReadFromTheRemoteThatHasIt(unittest.TestCase):
    def remote_of(self, *remotes: str) -> str:
        with repo_with(*remotes) as repo, mock.patch.object(world, "REPO", repo):
            return world._main_remote()

    def test_a_checkout_with_only_origin_reads_main_from_origin(self):
        self.assertEqual(self.remote_of("origin"), "origin")

    def test_a_fork_checkout_reads_main_from_upstream(self):
        self.assertEqual(self.remote_of("origin", "upstream"), "upstream")


if __name__ == "__main__":
    unittest.main()
