import os
import subprocess
import tempfile
import unittest
from unittest import mock

from nzfleet import state, tick, world


class ForkMainSync(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        state.PATH = os.path.join(self.home, "state.json")
        state.LOCK = os.path.join(self.home, "state.lock")

    def test_origin_repository_reads_https_and_ssh_remote_urls(self):
        for url in ("https://github.com/example-fork/nzela.git",
                    "git@github.com:example-fork/nzela.git"):
            with self.subTest(url=url), mock.patch.object(world, "git", return_value=url):
                self.assertEqual(world.origin_repository(), "example-fork/nzela")

    def test_sync_skips_an_upstream_only_checkout(self):
        with mock.patch.object(world, "git", return_value="https://github.com/nzela-org/nzela.git"), \
                mock.patch.object(world, "gh") as gh:
            self.assertFalse(world.sync_fork_main())
        gh.assert_not_called()

    def test_sync_refuses_an_origin_url_with_extra_components(self):
        with mock.patch.object(world, "git", return_value="https://github.com/owner/repo?target=other"), \
                mock.patch.object(world, "gh") as gh:
            with self.assertRaises(world.Failed):
                world.sync_fork_main()
        gh.assert_not_called()

    def test_sync_uses_gh_fast_forward_and_never_force(self):
        with mock.patch.object(world, "git", return_value="git@github.com:example-fork/nzela.git"), \
                mock.patch.object(world, "gh") as gh:
            self.assertTrue(world.sync_fork_main())
        gh.assert_called_once_with("repo", "sync", "example-fork/nzela",
                                   "--source", world.UPSTREAM, "--branch", "main")
        self.assertNotIn("--force", gh.call_args.args)

    def test_a_diverged_fork_sync_failure_is_reported_without_force(self):
        with mock.patch.object(world, "git", return_value="git@github.com:example-fork/nzela.git"), \
                mock.patch.object(world, "gh", side_effect=world.Failed("not fast-forward")) as gh:
            with self.assertRaises(world.Failed):
                world.sync_fork_main()
        self.assertNotIn("--force", gh.call_args.args)

    def test_a_sync_timeout_is_retryable(self):
        with mock.patch.object(world, "git", return_value="git@github.com:fork/nzela.git"), \
                mock.patch.object(world, "gh", side_effect=subprocess.TimeoutExpired("gh", 600)):
            with self.assertRaises(world.Failed):
                world.sync_fork_main()

    def test_a_diverged_fork_keeps_main_unseen_and_retries_next_tick(self):
        with mock.patch.object(world, "main_sha", return_value="new-main"), \
                mock.patch.object(world, "merged_since", return_value=[42]), \
                mock.patch.object(world, "sync_fork_main", side_effect=[world.Failed("not fast-forward"), True]) as sync, \
                mock.patch.object(tick, "say"), mock.patch.object(tick, "tell_event") as tell:
            tick.merges(False)
            self.assertEqual(state.read()["main_seen"], "")
            tick.merges(False)

        self.assertEqual(state.read()["main_seen"], "new-main")
        self.assertEqual(sync.call_count, 2)
        tell.assert_not_called()

    def test_first_tick_sync_failure_retries_without_advancing_seen(self):
        with mock.patch.object(world, "main_sha", return_value="new-main"), \
                mock.patch.object(world, "sync_fork_main", side_effect=world.Failed("offline")) as sync, \
                mock.patch.object(tick, "say"), mock.patch.object(tick, "tell_event") as tell:
            tick.merges(False)

        self.assertEqual(state.read()["main_seen"], "")
        sync.assert_called_once_with()
        tell.assert_not_called()

    def test_dry_run_does_not_sync_or_advance_seen(self):
        with mock.patch.object(world, "main_sha", return_value="new-main"), \
                mock.patch.object(world, "sync_fork_main") as sync, \
                mock.patch.object(tick, "say"):
            tick.merges(True)

        sync.assert_not_called()
        self.assertEqual(state.read()["main_seen"], "")

    def test_unchanged_main_does_not_sync_again(self):
        with state.change() as data:
            data["main_seen"] = "same-main"
        with mock.patch.object(world, "main_sha", return_value="same-main"), \
                mock.patch.object(world, "sync_fork_main") as sync:
            tick.merges(False)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
