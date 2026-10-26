import os
import unittest
from types import SimpleNamespace
from unittest import mock

from nzfleet import batch, cli, config, plan, refresh, state, tick, trainbuild, trainpr, trainrun, world

from fakeworld import CODE, SHOT, World, sh

COMPARISON = "frontend/admin/docs/comparison.html"
GENERATORS = {COMPARISON: f"echo regenerated > {COMPARISON}"}


class TrainWorld(unittest.TestCase):
    """Real git, a fake GitHub; the builds, the batch and the PR API answer from here."""

    def setUp(self):
        self.world = World().__enter__()
        self.addCleanup(self.world.__exit__, None, None, None)
        w = self.world
        self.opened, self.batches, self.batch_areas, self.broken = [], [], [], set()
        self.compiled = []
        for patch in (
                mock.patch.object(config, "WORK", w.dir),
                mock.patch.object(trainbuild, "compile_error", side_effect=self.compile_error),
                mock.patch.object(world, "issue", return_value=None),
                mock.patch.object(world, "create_pr", side_effect=self.create_pr),
                mock.patch.object(batch, "start_batch", side_effect=self.start_batch)):
            started = patch.start()
            self.addCleanup(patch.stop)
        w.write(COMPARISON, "base\n")
        w.commit("a comparison page")
        w.publish_main()

    def compile_error(self, tree):
        self.compiled.append(tree)
        return "error[E0425]: cannot find value `x`" if any(os.path.exists(os.path.join(tree, f)) for f in self.broken) else ""

    def start_batch(self, prs, areas=None):
        self.batches.append(prs)
        self.batch_areas.append(areas)

    def create_pr(self, branch, title, body):
        self.opened.append((branch, title, body))
        return 900 + len(self.opened)

    def pushed(self, path):
        branch = self.opened[-1][0]
        return sh(self.world.origin, "git", "show", f"{branch}:{path}")

    def train(self, *numbers):
        return trainrun.run(list(numbers), GENERATORS, say=lambda line: None)


class ReadyTrainPR(unittest.TestCase):
    def test_a_train_pull_request_opens_ready_for_review(self):
        with mock.patch.object(world, "gh", return_value="https://github.com/nzela-org/nzela/pull/901") as gh:
            self.assertEqual(world.create_pr("train-2026-10-15-1", "Train", "Body"), 901)
        self.assertNotIn("--draft", gh.call_args.args)


class Train(TrainWorld):

    def test_members_missing_from_the_final_assembly_reach_status_and_owner_mail(self):
        final = SimpleNamespace(left_out=[(2, [CODE])], merged=[3])
        left_out = trainrun._left_out([(1, [])], [2, 3], final)

        self.assertEqual(left_out, [(1, []), (2, [CODE])])
        record = {"members": [3], "left_out": [number for number, _ in left_out], "dropped": [],
                  "compiled": True, "reviewed": "", "mailed": False, "mailing": False}
        row = trainpr.status_row("901", record, {})
        self.assertEqual(row["left_out"], [1, 2])
        _, body = trainpr.letter(901, [3], left_out, [], "nzela-org/nzela", "head")
        self.assertIn("Left out: #1, #2.", body)

    def test_a_member_dropped_by_the_final_merge_is_not_called_a_conflict(self):
        reason = trainpr.left_out_reason([])
        self.assertNotIn("conflict", reason)
        self.assertIn("- #2: " + reason, trainpr.body([3], [], [(2, reason)], [], [], []))
        self.assertIn("missing from the final merge", trainpr.rebase_brief(2, 901, []))
        self.assertIn(CODE, trainpr.rebase_brief(2, 901, [CODE]))

    def test_run_records_and_words_a_compiled_member_the_final_merge_omitted(self):
        w = self.world
        w.open_pr(1, {"crates/nzela/src/one.rs": "one\n"})
        w.open_pr(2, {"crates/nzela/src/two.rs": "two\n"})
        real = trainrun.assemble

        def omit_two_at_the_end(name, numbers, heads, generators):
            made = real(name, numbers, heads, generators)
            if numbers == [1, 2] and self.compiled:
                made.merged = [n for n in made.merged if n != 2]
            return made

        with mock.patch.object(trainrun, "assemble", side_effect=omit_two_at_the_end):
            self.assertEqual(self.train(1, 2), 0)

        record = state.read()["train_prs"]["901"]
        self.assertEqual(record["left_out"], [2])
        self.assertEqual(record["members"], [1])
        body = self.opened[-1][2]
        self.assertIn("- #2: " + trainpr.left_out_reason([]), body)
        self.assertNotIn("conflict in", body)
        queued = [t for t in state.read()["todo"] if t["key"] == "rebase-2"]
        self.assertIn("missing from the final merge", queued[0]["task"])

    def test_an_open_postsubmit_failure_allows_a_repair_train(self):
        self.world.open_pr(1298, {"crates/nzela/src/member.rs": "member\n"})
        with mock.patch.object(world, "issue", return_value={"number": 42}):
            self.assertEqual(self.train(1298), 0)
        self.assertEqual(len(self.opened), 1)

    def test_an_open_postsubmit_failure_allows_a_proved_release(self):
        with state.change() as s:
            s["train_prs"]["901"] = {"members": [1298], "left_out": [], "dropped": [],
                                       "reviewed": "", "mailed": False, "mailing": False}
        with mock.patch.object(world, "issue", return_value={"number": 42}), \
                mock.patch.object(world, "fetch"), mock.patch.object(world, "head_of", return_value="proved"), \
                mock.patch.object(trainrun, "_waiting", return_value=""), \
                mock.patch.object(world, "mark_ready"), mock.patch.object(trainrun.mail, "send", return_value=True) as send:
            self.assertEqual(trainrun.release(901), 0)
        send.assert_called_once()

    def test_a_recent_registered_train_draft_keeps_its_exact_head_in_the_shape_snapshot(self):
        w = self.world
        w.open_pr(1298, {"crates/nzela/src/member.rs": "member\n"})
        source = w.head(1298)
        w.pinned[1298] = (source, "CONFLICTING")
        self.assertEqual(self.train(1298), 0)
        train_pr = 901
        branch = self.opened[-1][0]
        w.prs[train_pr] = branch
        w.branches[train_pr] = branch
        w.draft[train_pr] = True
        record = state.read()["train_prs"][str(train_pr)]
        with state.change() as s:
            s["batches"]["native"] = {"status": "running", "pid": 456,
                                        "members": [[train_pr, record["head"]]]}
        original_open_prs = world.open_prs

        def recently_updated_train():
            prs = original_open_prs()
            for pr in prs:
                if pr["number"] == train_pr:
                    pr["updatedAt"] = tick.datetime.datetime.now(tick.datetime.timezone.utc).isoformat()
            return prs

        with mock.patch.object(world, "open_prs", side_effect=recently_updated_train), \
                mock.patch.object(tick.agents, "_alive", return_value=True), \
                mock.patch.object(tick.subprocess, "Popen") as popen:
            prs = tick.read()
            self.assertEqual({pr.number: pr.sha for pr in prs}[train_pr], record["head"])
            tick.shape(prs, {}, False)
        popen.assert_not_called()
        self.assertEqual(w.head(1298), source)

        sh(w.repo, "git", "checkout", "-q", "-B", branch, f"origin/{branch}")
        w.write("crates/nzela/src/train-change.rs", "train changed\n")
        w.commit("train head changes")
        sh(w.repo, "git", "push", "-q", "-f", "origin", f"{branch}:{branch}")
        sh(w.repo, "git", "checkout", "-q", "main")
        with mock.patch.object(world, "open_prs", side_effect=recently_updated_train), \
                mock.patch.object(tick.agents, "_alive", return_value=True), \
                mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=789)) as popen:
            prs = tick.read()
            self.assertNotEqual({pr.number: pr.sha for pr in prs}[train_pr], record["head"])
            tick.shape(prs, {}, False)
        popen.assert_called_once()

        w.open_pr(1298, {"crates/nzela/src/member.rs": "source changed\n"})
        w.pinned[1298] = (w.head(1298), "CONFLICTING")
        with mock.patch.object(world, "open_prs", side_effect=recently_updated_train), \
                mock.patch.object(tick.agents, "_alive", return_value=True), \
                mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=790)) as popen:
            prs = tick.read()
            self.assertNotEqual({pr.number: pr.sha for pr in prs}[1298], source)
            tick.shape(prs, {}, False)
        popen.assert_called_once()

    def test_a_running_native_train_batch_holds_its_assembled_source_head(self):
        from types import SimpleNamespace

        w = self.world
        w.open_pr(1298, {"crates/nzela/src/member.rs": "member\n"})
        source = w.head(1298)
        with mock.patch.object(world, "create_pr", return_value=1299):
            self.assertEqual(self.train(1298), 0)
        train_pr = 1299
        record = state.read()["train_prs"][str(train_pr)]
        self.assertEqual(record["source_heads"], {"1298": source})
        self.assertTrue(record["head"])
        with state.change() as s:
            s["batches"]["native"] = {"status": "running", "pid": 456,
                                        "members": [[train_pr, record["head"]]]}
        current_source = w.head(1298)
        self.assertEqual(current_source, source)
        pr = plan.Pr(1298, current_source, frozenset({"rust"}), False, True, False)
        train_head = plan.Pr(train_pr, record["head"], frozenset({"rust"}), False, False, False)
        with mock.patch.object(tick.agents, "_alive", return_value=True), \
                mock.patch.object(tick.subprocess, "Popen") as popen:
            tick.shape([pr, train_head], {}, False)
        popen.assert_not_called()
        edited = plan.Pr(1298, "independently-edited-head", frozenset({"rust"}), False, True, False)
        with mock.patch.object(tick.agents, "_alive", return_value=True), \
                mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=789)) as popen:
            tick.shape([edited, train_head], {}, False)
        popen.assert_called_once()

    def test_screenshots_that_conflict_keep_the_later_copy_for_the_batch(self):
        w = self.world
        w.open_pr(1, {SHOT: "one\n"})
        w.open_pr(2, {SHOT: "two\n"})
        self.assertEqual(self.train(1, 2), 0)
        self.assertEqual(self.pushed(SHOT), "two")
        self.assertIn("#2: 1 screenshot(s) conflicted", self.opened[0][2])
        self.assertEqual(self.opened[0][1], "2 open changes land together")

    def test_a_train_opens_without_capturing_its_screenshots(self):
        w = self.world
        w.open_pr(1, {"frontend/admin/src/Screen.tsx": "screen\n"})
        w.open_pr(2, {"frontend/android/app/src/main/Screen.kt": "screen\n"})
        with mock.patch.object(trainbuild, "redraw") as redraw:
            self.assertEqual(self.train(1), 0)
            self.assertEqual(self.train(2), 0)
        redraw.assert_not_called()
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.batches, [[901], [902]])
        self.assertIn("refreshed by the batch", self.opened[0][2])

    def test_the_batch_refresh_commits_screenshots_on_the_train_branch(self):
        w = self.world
        w.open_pr(1, {"frontend/admin/src/Screen.tsx": "screen\n"})
        self.assertEqual(self.train(1), 0)
        branch = self.opened[0][0]
        w.prs[901] = w.branches[901] = branch
        head = w.head(901)
        shots = os.path.join(w.dir, "shots")
        before = os.path.join(shots, "console", "before", SHOT)
        after = os.path.join(shots, "console", "after", SHOT)
        os.makedirs(os.path.dirname(before))
        os.makedirs(os.path.dirname(after))
        with open(before, "wb") as image:
            image.write(b"before")
        with open(after, "wb") as image:
            image.write(b"after")
        with open(os.path.join(shots, "console", "changed.txt"), "w") as changes:
            changes.write(f"changed\t{SHOT}\n")

        with mock.patch.object(refresh, "WORK", w.dir), \
                mock.patch.object(refresh, "golden", return_value=set()), \
                mock.patch.object(batch, "_main_shots", return_value=""):
            note, heads = batch._refresh_shots("train-refresh", [901], {901: head}, {"shots": shots})

        self.assertIn("committed on #901", note)
        self.assertIn(901, heads)
        self.assertEqual(sh(w.origin, "git", "show", f"{branch}:{SHOT}"), "after")
        self.assertIn("Screenshots for train train-refresh",
                      sh(w.origin, "git", "log", "-1", "--format=%s", branch))

    def test_a_train_that_follows_a_refusal_clears_it(self):
        w = self.world
        with state.change() as s:
            s["train_refused"] = {"why": "old capture failure"}
        w.open_pr(1, {"crates/nzela/src/a.rs": "change\n"})
        self.train(1)
        self.assertEqual(state.read()["train_refused"], {})

    def test_a_generated_file_that_conflicts_is_made_again_on_the_merged_tree(self):
        w = self.world
        w.open_pr(1, {COMPARISON: "one\n"})
        w.open_pr(2, {COMPARISON: "two\n"})
        self.train(1, 2)
        self.assertEqual(self.pushed(COMPARISON), "regenerated")

    def test_a_source_conflict_leaves_the_change_out_and_queues_its_rebase_behind_the_train(self):
        w = self.world
        w.open_pr(1, {CODE: "one\n"})
        w.open_pr(2, {CODE: "two\n"})
        w.open_pr(3, {"crates/nzela/src/c.rs": "three\n"})
        self.train(1, 2, 3)
        self.assertEqual(self.pushed("crates/nzela/src/c.rs"), "three")
        self.assertEqual(self.pushed(CODE), "one")
        task, = [t for t in state.read()["todo"] if t["key"] == "rebase-2"]
        self.assertEqual(task["after"], ["pr:901"])
        self.assertIn(CODE, task["task"])
        self.assertIn("#2", self.opened[0][2])

    def test_a_change_that_breaks_the_build_is_dropped_and_gets_a_fix_task(self):
        w = self.world
        self.broken = {"crates/nzela/src/bad.rs"}
        w.open_pr(1, {"crates/nzela/src/a.rs": "one\n"})
        w.open_pr(2, {"crates/nzela/src/bad.rs": "bad\n"})
        w.open_pr(3, {"crates/nzela/src/c.rs": "three\n"})
        self.train(1, 2, 3)
        self.assertEqual(self.pushed("crates/nzela/src/c.rs"), "three")
        self.assertEqual(sh(w.origin, "git", "ls-tree", "-r", "--name-only", self.opened[0][0]).count("bad.rs"), 0)
        task, = [t for t in state.read()["todo"] if t["key"] == "fix-compile-2"]
        self.assertIn("E0425", task["task"])

    def test_nothing_to_land_opens_no_pull_request(self):
        self.assertEqual(self.train(), 0)
        self.assertEqual((self.opened, self.batches, self.world.mails), ([], [], []))

    def test_a_new_train_is_checked_but_neither_mailed_nor_made_ready(self):
        w = self.world
        w.open_pr(1, {CODE: "one\n"})
        self.train(1)
        self.assertEqual((self.batches, w.mails, w.ready), ([[901]], [], []))

    def test_the_scratch_worktrees_are_gone_afterwards(self):
        self.world.open_pr(1, {CODE: "one\n"})
        self.train(1)
        self.assertEqual([l for l in sh(self.world.repo, "git", "worktree", "list").splitlines() if "nzf-train" in l], [])


class ChangesToHowChecksRunLeaveAlone(TrainWorld):
    WORKFLOW = ".github/workflows/ci.yaml"

    def test_a_pipeline_change_travels_without_the_others_and_the_train_says_so(self):
        self.world.open_pr(1, {CODE: "one\n"})
        self.world.open_pr(2, {self.WORKFLOW: "name: ci\n"})
        self.train(1, 2)
        self.assertEqual(state.read()["train_prs"]["901"]["members"], [2])
        self.assertIn(trainpr.CI_NOTICE, self.opened[0][2])
        self.assertTrue(state.read()["train_prs"]["901"]["ci"])

    def test_a_repair_train_forms_while_the_last_pipeline_change_has_no_passing_main_run(self):
        self.world.open_pr(1, {CODE: "one\n"})
        with mock.patch.object(trainrun, "_last_ci_change", return_value="c" * 40):
            self.assertEqual(self.train(1), 0)
        self.assertEqual(state.read()["train_prs"]["901"]["members"], [1])
        self.assertTrue(trainpr.ci_proof_areas("c" * 40, {}, world.is_ancestor) <= set(self.batch_areas[0]))


class AfterTheTrain(TrainWorld):
    def test_a_red_verdict_queues_a_fix_task_for_each_failing_area_with_the_log_lines(self):
        self.world.open_pr(1, {CODE: "one\n"})
        self.train(1)
        with state.change() as s:
            s["failed"]["901"] = {"sha": "abc", "why": "failed: rust console", "areas": ["rust", "console"], "log": "panicked at a.rs:3"}
        trainrun.queue_red_fixes()
        trainrun.queue_red_fixes()
        keys = [t["key"] for t in state.read()["todo"] if t["key"].startswith("train-fix-")]
        self.assertEqual(keys, ["train-fix-901-rust", "train-fix-901-console"])
        self.assertIn("panicked at a.rs:3", state.read()["todo"][0]["task"])

    def test_status_shows_who_is_in_and_the_batch_verdict(self):
        self.world.open_pr(1, {CODE: "one\n"})
        self.train(1)
        with state.change() as s:
            s["batches"]["b"] = {"members": [[901, "x"]], "started": 1.0, "status": "green"}
        row, = [trainpr.status_row(pr, r, state.read()["batches"]) for pr, r in state.read()["train_prs"].items()]
        self.assertEqual((row["members"], row["verdict"]), ([1], "green"))


class TheMailWaitsForTheEvidence(TrainWorld):
    HEAD, TREE = "h" * 40, "t" * 40

    def setUp(self):
        super().setUp()
        self.world.open_pr(1, {CODE: "one\n"})
        self.train(1)
        self.files = ["crates/a.rs"]
        for patch in (
                mock.patch.object(world, "fetch"),
                mock.patch.object(world, "head_of", side_effect=lambda n: self.HEAD),
                mock.patch.object(world, "changed_files", side_effect=lambda sha: self.files),
                mock.patch.object(world, "areas", side_effect=lambda files, tier="change": frozenset({"rust"})),
                mock.patch.object(world, "verified_tree", side_effect=lambda members: self.TREE)):
            patch.start()
            self.addCleanup(patch.stop)
        self.pass_batch()

    @staticmethod
    def proof(tree, *areas):
        return {"tree": tree, "areas": list(areas), "runs": {area: "b-r0" for area in areas}}

    def pass_batch(self, **changes):
        batch_record = {"members": [[901, self.HEAD]], "started": 1.0, "status": "green", "passed": [901], "skipped": [],
                        "proved": self.proof(self.TREE, "rust", plan.GATE), **changes}
        with state.change() as s:
            s["batches"]["b"] = batch_record
            s["passed"]["901"] = {"sha": self.HEAD, "main": "m", "order": [901], "batch": "b"}

    def mails(self):
        trainrun.release(901)
        return self.world.mails

    def test_a_complete_pass_and_a_release_mail_one_command_and_make_it_ready(self):
        w = self.world
        self.assertEqual(self.mails()[0][1].count("gh pr merge 901"), 1)
        self.assertEqual(w.ready, [901])

    def test_an_unproven_pipeline_change_waits_for_a_full_exact_tree_pass_before_mail(self):
        with state.change() as s:
            s["main_ci"] = {"sha": "m", "ok": False}
        with mock.patch.object(trainrun, "_last_ci_change", return_value="c" * 40), \
                mock.patch.object(world, "is_ancestor", return_value=False):
            self.assertEqual(self.mails(), [])
            required = trainpr.ci_proof_areas("c" * 40, {"sha": "m", "ok": False}, world.is_ancestor)
            self.pass_batch(proved=self.proof(self.TREE, *sorted(required)))
            self.assertEqual(len(self.mails()), 1)

    def test_a_train_is_mailed_only_once(self):
        self.mails()
        trainrun.release(901)
        trainrun.mail_released_trains()
        self.assertEqual(len(self.world.mails), 1)

    def test_ready_and_mail_happen_only_after_the_review_and_tree_gate(self):
        events = []
        with mock.patch.object(world, "mark_ready", side_effect=lambda pr: events.append(("ready", pr))), \
                mock.patch.object(trainrun.mail, "send", side_effect=lambda subject, body: events.append(("mail", body)) or True):
            trainrun.release(901)
        self.assertEqual([event[0] for event in events], ["ready", "mail"])

    def test_an_interrupted_notification_is_not_sent_a_second_time_automatically(self):
        with state.change() as s:
            s["train_prs"]["901"]["mailing"] = True
        trainrun.mail_released_trains()
        self.assertEqual((self.world.mails, self.world.ready), ([], []))
        row, = [trainpr.status_row(pr, record, state.read()["batches"])
                for pr, record in state.read()["train_prs"].items()]
        self.assertEqual(row["notice"], "mail outcome uncertain")

    def test_a_failed_mail_can_be_retried_without_marking_the_train_mailed(self):
        with mock.patch.object(trainrun.mail, "send", side_effect=[False, True]) as send:
            trainrun.release(901)
            self.assertFalse(state.read()["train_prs"]["901"]["mailed"])
            trainrun.mail_released_trains()
        self.assertEqual(send.call_count, 2)
        self.assertTrue(state.read()["train_prs"]["901"]["mailed"])

    def test_a_pass_without_a_release_mails_nothing(self):
        trainrun.mail_released_trains()
        self.assertEqual((self.world.mails, self.world.ready), ([], []))

    def test_a_release_without_a_pass_mails_nothing(self):
        with state.change() as s:
            s["passed"].clear()
        self.assertEqual((self.mails(), self.world.ready), ([], []))

    def test_a_failed_or_pending_batch_is_not_mailed(self):
        for status in ("red", "running", "partial"):
            self.pass_batch(status=status)
            self.assertEqual(self.mails(), [], status)

    def test_a_skipped_member_is_not_mailed(self):
        self.pass_batch(skipped=[901])
        self.assertEqual(self.mails(), [])

    def test_a_configured_skipped_required_area_blocks_release(self):
        self.pass_batch(proved=self.proof(self.TREE, plan.GATE))
        with mock.patch.object(config, "SKIP_AREAS", frozenset({"rust"})):
            self.assertEqual(self.mails(), [])

    def test_a_head_that_changed_after_the_pass_is_not_mailed(self):
        with state.change() as s:
            s["passed"]["901"]["sha"] = "o" * 40
        self.assertEqual(self.mails(), [])

    def test_a_release_of_an_older_head_does_not_cover_a_newer_one(self):
        with state.change() as s:
            s["train_prs"]["901"]["reviewed"] = "o" * 40
        trainrun.mail_released_trains()
        self.assertEqual(self.world.mails, [])

    def test_a_pass_on_another_tree_is_not_mailed(self):
        self.pass_batch(proved=self.proof("x" * 40, "rust", plan.GATE))
        self.assertEqual(self.mails(), [])

    def test_a_green_batch_with_a_flake_waits_for_a_fresh_stable_pass(self):
        self.pass_batch(flaky=["worker::tests::slow"])
        self.assertEqual(self.mails(), [])
        self.pass_batch()
        self.assertEqual(len(self.mails()), 1)

    def test_a_frontend_train_needs_both_frontends_checked_on_the_tree(self):
        self.files = ["frontend/admin/src/a.ts", "frontend/android/app/B.kt"]
        self.assertEqual(self.mails(), [])
        self.pass_batch(proved=self.proof(self.TREE, "rust", plan.GATE, "console", "android"))
        self.assertEqual(len(self.mails()), 1)

    def test_the_general_ready_queue_does_not_offer_a_train(self):
        from nzfleet import tick
        prs = [mock.Mock(number=901, areas=frozenset({"rust"}), draft=True)]
        with mock.patch.object(world, "merged_since", return_value=[]), mock.patch.object(world, "merges_cleanly", return_value=[]):
            self.assertEqual(tick.ready(prs, False), [])

    def test_the_train_release_command_does_not_replace_held_work_release(self):
        with mock.patch.object(trainrun, "release", return_value=0) as release:
            self.assertEqual(cli.main(["release-train", "901"]), 0)
        release.assert_called_once_with(901)
