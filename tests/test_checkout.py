import unittest

from fakeworld import World, sh, real_repo_is_bare
from nzfleet import checkout


class Bare(unittest.TestCase):
    def test_a_tick_sets_a_bare_main_checkout_back_to_a_work_tree(self):
        with World() as w:
            sh(w.repo, "git", "config", "core.bare", "true")
            w.tick()
            self.assertEqual(sh(w.repo, "git", "config", "core.bare"), "false")
            self.assertTrue(any("core.bare was true" in line for line in w.said))

    def test_a_tick_says_nothing_about_a_healthy_checkout(self):
        with World() as w:
            w.tick()
            self.assertFalse(any("core.bare" in line for line in w.said))

    def test_a_dry_run_reports_and_leaves_it(self):
        with World() as w:
            sh(w.repo, "git", "config", "core.bare", "true")
            self.assertIn("would set it back", checkout.repair(dry_run=True))
            self.assertEqual(sh(w.repo, "git", "config", "core.bare"), "true")

    def test_the_suite_does_not_turn_the_real_repository_bare(self):
        with World():
            self.assertFalse(real_repo_is_bare())


if __name__ == "__main__":
    unittest.main()
