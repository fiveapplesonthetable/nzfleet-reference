import os
import tempfile
import unittest
from unittest import mock

from nzfleet import batch, config, state, statuses, world
from tests.test_batch import FakeChecks

TREE = "a" * 40


class Sent:
    """A sender that remembers every (sha, state, description) and the context it was posted under."""

    def __init__(self, fail=False):
        self.posts, self.contexts, self.fail = [], [], fail

    def __call__(self, sha, status, description, context=statuses.CONTEXT):
        if self.fail:
            raise OSError("GitHub answered 500")
        self.posts.append((sha, status, description))
        self.contexts.append(context)

    def states(self, sha):
        return [status for (s, status, _), c in zip(self.posts, self.contexts) if s == sha and c == statuses.CONTEXT]


class Statuses(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        state.PATH, state.LOCK = os.path.join(home, "state.json"), os.path.join(home, "state.lock")
        main = mock.patch.object(world, "MAIN", "HEAD")
        main.start()
        self.addCleanup(main.stop)
        self.sent = Sent()

    def start(self, members, batch_id="b"):
        with state.change() as s:
            s["batches"][batch_id] = {"group": "core", "members": [[n, f"s{n}"] for n in members], "areas": ["rust"],
                                      "main": "m", "started": 0, "status": "running", "pid": 0}

    def run_batch(self, checks, batch_id="b"):
        batch.run(batch_id, areas_of=lambda shas: frozenset(), check=checks, send=self.sent)

    def test_a_batch_marks_every_head_pending_before_it_checks_anything(self):
        self.start([1, 2])
        self.run_batch(FakeChecks())
        self.assertEqual([p[:2] for p in self.sent.posts[:2]], [("s1", "pending"), ("s2", "pending")])

    def test_a_green_batch_marks_every_member_success_on_the_head_it_checked(self):
        self.start([1, 2])
        self.run_batch(FakeChecks())
        self.assertEqual((self.sent.states("s1"), self.sent.states("s2")), (["pending", "success"], ["pending", "success"]))

    def test_the_pull_request_that_broke_the_batch_fails_and_the_others_succeed(self):
        self.start([1, 2, 3])
        self.run_batch(FakeChecks(broken={2}))
        self.assertEqual([self.sent.states(f"s{n}")[-1] for n in (1, 2, 3)], ["success", "failure", "success"])

    def test_a_pull_request_that_does_not_merge_fails(self):
        self.start([1, 2])
        self.run_batch(FakeChecks(skip={2}))
        self.assertEqual([self.sent.states(f"s{n}")[-1] for n in (1, 2)], ["success", "failure"])

    def test_a_batch_that_could_not_run_posts_error_not_success(self):
        self.start([1, 2])
        self.run_batch(FakeChecks(infrastructure=True))
        self.assertEqual([self.sent.states(f"s{n}")[-1] for n in (1, 2)], ["error", "error"])

    def test_a_batch_where_main_is_already_broken_posts_error_for_every_member(self):
        self.start([1, 2])
        fails_everywhere = FakeChecks()
        fails_everywhere.broken = {0, 1, 2}
        fails_everywhere._result = lambda rc, merged, log: {
            "rc": 1, "merged": merged, "skipped": [], "line": "failed: rust", "log": "test money::tests::adds ... FAILED"}
        self.run_batch(fails_everywhere)
        self.assertEqual(state.read()["batches"]["b"]["status"], "main-broken")
        self.assertEqual([self.sent.states(f"s{n}")[-1] for n in (1, 2)], ["error", "error"])

    def test_a_batch_that_crashes_posts_error_and_still_raises(self):
        self.start([1])
        def crash(name, areas, members):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_batch(crash)
        self.assertEqual(self.sent.states("s1"), ["pending", "error"])

    def test_a_failed_post_does_not_stop_the_batch(self):
        self.start([1])
        self.sent = Sent(fail=True)
        self.run_batch(FakeChecks())
        self.assertEqual(state.read()["batches"]["b"]["status"], "green")

    def test_descriptions_fit_what_github_accepts(self):
        sent = Sent()
        statuses.post_all([(1, "s1")], lambda pr: ("error", "x" * 500), sent)
        self.assertEqual(len(sent.posts[0][2]), statuses.LIMIT)

    def test_without_a_token_file_nothing_is_posted_and_setup_says_so(self):
        with mock.patch.object(config, "STATUS_TOKEN_FILE", ""):
            self.assertIsNone(statuses.GitHub.from_config())
        self.assertIn("NZFLEET_STATUS_TOKEN_FILE", statuses.setup_advice("")[0])
        self.assertEqual(statuses.setup_advice("/some/file"), [])

    def test_the_token_is_read_from_the_configured_file(self):
        path = os.path.join(tempfile.mkdtemp(), "token")
        with open(path, "w") as f:
            f.write("ghp_secret\n")
        with mock.patch.object(config, "STATUS_TOKEN_FILE", path):
            self.assertEqual(statuses.GitHub.from_config().token, "ghp_secret")


    def checks(self, **kinds):
        checks = FakeChecks(**kinds)
        plain = checks._result
        checks._result = lambda rc, merged, log: {**plain(rc, merged, log), "tree": TREE}
        return checks

    def test_a_green_batch_posts_only_its_merge_check(self):
        self.start([1, 2])
        self.run_batch(self.checks())
        self.assertEqual(set(self.sent.contexts), {statuses.CONTEXT})

    def test_a_rerun_after_a_flake_proves_only_the_areas_it_ran(self):
        self.start([1, 2])
        with state.change() as s:
            s["batches"]["b"]["areas"] = ["rust", "console"]
        checks = self.checks(flaky=True)
        self.run_batch(checks)
        self.assertEqual(state.read()["batches"]["b"]["proved"], {"tree": TREE, "areas": sorted(checks.areas[-1]),
                          "runs": {a: "b-r0-again" for a in sorted(checks.areas[-1])}})
