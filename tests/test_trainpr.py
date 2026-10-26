import unittest
import tempfile
from unittest import mock

from nzfleet import jev, trainpr, trainrun, world
from nzfleet.trainpr import Candidate

SHOT = "frontend/admin/screenshots/a.png"
GENERATORS = {"frontend/admin/docs/comparison.html": "make"}


def pr(n, *files):
    return Candidate(n, files)


class Order(unittest.TestCase):
    def test_changes_without_frontend_files_go_first_and_smaller_before_larger(self):
        got = trainpr.order([pr(1, "frontend/admin/src/a.tsx"), pr(2, "a.rs", "b.rs"), pr(3, "c.rs")])
        self.assertEqual(got, [3, 2, 1])

    def test_changes_to_the_same_screen_are_adjacent(self):
        got = trainpr.order([pr(1, "frontend/admin/src/a/x.tsx"), pr(2, "frontend/admin/src/b/y.tsx"),
                             pr(3, "frontend/admin/src/a/z.tsx", "frontend/admin/src/a/w.tsx")])
        self.assertEqual(got, [1, 3, 2])


class JevShadow(unittest.TestCase):
    def test_train_membership_is_logged_beside_the_existing_order(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.object(jev.config, "HOME", directory.name), \
             mock.patch.object(world, "changed_files", side_effect=lambda _sha: ["crates/a.rs"]), \
             mock.patch.object(world, "areas", return_value=frozenset({"rust"})), \
             mock.patch.object(trainrun.jev, "TypeSafe", return_value=jev.Rule(lambda _state, menu: menu[0])):
            trainrun._shadow_membership([1, 2], {1: "a", 2: "b"})
        record = jev.Log(directory.name).read()[0]
        self.assertEqual((record["point"], record["rule"], record["jev"], record["acted"]),
                         ("train.membership", "add:1", "add:1", "rule"))


class Settling(unittest.TestCase):
    def test_a_screenshot_is_taken_from_the_later_change(self):
        self.assertEqual(trainpr.settle([SHOT], GENERATORS).take, (SHOT,))

    def test_a_generated_file_is_taken_and_drawn_again(self):
        got = trainpr.settle(["frontend/admin/docs/comparison.html"], GENERATORS)
        self.assertEqual((got.take, got.regenerate), (("frontend/admin/docs/comparison.html",), ("frontend/admin/docs/comparison.html",)))

    def test_one_source_file_leaves_the_change_out(self):
        self.assertEqual(trainpr.settle([SHOT, "crates/a.rs"], GENERATORS).by_hand, ("crates/a.rs",))


class ShotScreens(unittest.TestCase):
    def test_each_screen_is_named_once_by_frontend(self):
        got = trainpr.shot_screens([SHOT, SHOT, "frontend/android/docs/screenshots/Home.png", "frontend/admin/docs/comparison.html"])
        self.assertEqual(got, ["android: Home", "console: a"])


class DroppingBreakers(unittest.TestCase):
    def test_only_the_change_that_breaks_the_build_is_dropped(self):
        calls = []

        def compile_error(prefix):
            calls.append(list(prefix))
            return "error[E0308]" if 3 in prefix else ""

        self.assertEqual(trainpr.drop_breakers([1, 2, 3, 4, 5], compile_error), ([1, 2, 4, 5], [(3, "error[E0308]")]))

    def test_two_breakers_are_both_found(self):
        got = trainpr.drop_breakers([1, 2, 3, 4], lambda ns: "bad" if 2 in ns or 4 in ns else "")
        self.assertEqual(got, ([1, 3], [(2, "bad"), (4, "bad")]))

    def test_a_tree_that_compiles_is_built_once(self):
        calls = []
        trainpr.drop_breakers([1, 2], lambda ns: calls.append(ns) or "")
        self.assertEqual(calls, [[1, 2]])


class Eligibility(unittest.TestCase):
    OPEN = [{"number": n, "headRefName": b, "headRefOid": f"sha{n}"} for n, b in ((1, "a"), (2, "b"), (3, "c"), (4, "train-2026-10-10-1"), (5, "e"))]
    GREEN = {f"b{n}": {"status": "green", "members": [[n, f"sha{n}"]]} for n in (1, 2, 3, 4)}

    def test_running_held_and_train_pull_requests_are_not_taken(self):
        agents = {"x": {"status": "running", "pr": {"number": 1}}, "y": {"status": "done", "pr": {"number": 2}}}
        self.assertEqual(trainpr.eligible(self.OPEN, agents, {"y": {"why": "wait"}}, self.GREEN), [3])

    def test_a_pull_request_without_a_green_batch_of_its_own_is_not_taken(self):
        self.assertEqual(trainpr.eligible(self.OPEN, {}, {}, self.GREEN), [1, 2, 3])

    def test_a_green_batch_at_an_older_head_does_not_count(self):
        moved = {**self.GREEN, "b3": {"status": "green", "members": [[3, "old"]]}}
        self.assertEqual(trainpr.eligible(self.OPEN, {}, {}, moved), [1, 2])

    def test_a_red_batch_or_a_batch_of_several_does_not_count(self):
        batches = {"r": {"status": "red", "members": [[1, "sha1"]]}, "m": {"status": "green", "members": [[2, "sha2"], [3, "sha3"]]}}
        self.assertEqual(trainpr.eligible(self.OPEN, {}, {}, batches), [])


class Letter(unittest.TestCase):
    def test_the_mail_names_exactly_one_command(self):
        _, text = trainpr.letter(900, [1, 2], [(3, "conflict")], [], "nzela-org/nzela", "a" * 40)
        self.assertEqual([l for l in text.splitlines() if l.startswith("gh ") or l.startswith("nzfleet ")],
                         ["gh pr merge 900 -R nzela-org/nzela --merge --match-head-commit " + "a" * 40])

    def test_a_merge_command_pins_the_reviewed_head(self):
        command = trainpr.merge_command(900, "nzela-org/nzela", "b" * 40)
        self.assertEqual(command, "gh pr merge 900 -R nzela-org/nzela --merge --match-head-commit " + "b" * 40)


class WaitingForTheAreasToPass(unittest.TestCase):
    HEAD, TREE = "h" * 40, "t" * 40
    RECORD = {"mailed": False, "reviewed": HEAD}
    PASSED = {"sha": HEAD}

    def waiting(self, runs, required=("rust", "console")):
        batch = {"status": "green", "proved": {"tree": self.TREE, "areas": sorted(runs), "runs": runs}}
        return trainpr.waiting_for(self.RECORD, self.PASSED, batch, self.HEAD, self.TREE, frozenset(required))

    def test_a_train_is_not_ready_while_an_area_its_members_touch_has_no_passing_run(self):
        self.assertEqual(self.waiting({"rust": "b-r0"}), "no pass on this tree for console")

    def test_a_train_is_ready_once_each_required_area_names_the_run_that_proved_it(self):
        self.assertEqual(self.waiting({"rust": "b-r0", "console": "b-r0-again"}), "")

    def test_an_area_listed_without_a_run_does_not_count(self):
        batch = {"status": "green", "proved": {"tree": self.TREE, "areas": ["rust", "console"], "runs": {"rust": "b-r0"}}}
        got = trainpr.waiting_for(self.RECORD, self.PASSED, batch, self.HEAD, self.TREE, frozenset({"rust", "console"}))
        self.assertEqual(got, "no pass on this tree for console")


class ChangesToHowChecksRun(unittest.TestCase):
    def test_a_workflow_change_is_a_train_of_its_own(self):
        got = trainpr.solo_ci([pr(1, "crates/a.rs"), pr(2, ".github/workflows/ci.yaml"), pr(3, "scripts/checks.sh")])
        self.assertEqual([c.number for c in got], [2])

    def test_each_pipeline_file_counts(self):
        for path in (".github/workflows/ci.yaml", "scripts/checks.sh", "scripts/areas/rust.sh", "tools/nzfleet/nzfleet/selection/areas.py"):
            self.assertEqual([c.number for c in trainpr.solo_ci([pr(1, "a.rs"), pr(2, path)])], [2], path)

    def test_other_scripts_do_not_make_a_train_solo(self):
        got = trainpr.solo_ci([pr(1, "scripts/other-tool.sh"), pr(2, "crates/a.rs")])
        self.assertEqual([c.number for c in got], [1, 2])

    def test_an_unproven_pipeline_change_requires_a_full_batch_for_the_repair(self):
        ancestors = {("c1", "m1"), ("c1", "m2")}
        is_ancestor = lambda a, b: (a, b) in ancestors
        required = trainpr.ci_proof_areas("c1", {"sha": "m1", "ok": False}, is_ancestor)
        self.assertIn("rust", required)
        self.assertIn("android-release", required)
        self.assertIn("console-browser", required)
        self.assertEqual(trainpr.ci_proof_areas("c1", {"sha": "m0", "ok": True}, is_ancestor), required)
        self.assertEqual(trainpr.ci_proof_areas("c1", {"sha": "m2", "ok": True}, is_ancestor), frozenset())
        self.assertEqual(trainpr.ci_proof_areas("", {"sha": "m1", "ok": False}, is_ancestor), frozenset())

    def test_the_letter_explains_why_following_trains_need_full_proof(self):
        _, text = trainpr.letter(900, [2], [], [], "nzela-org/nzela", "a" * 40, ci=True)
        self.assertIn(trainpr.CI_NOTICE, text)
