from types import SimpleNamespace
import unittest
import os
import tempfile
from unittest import mock

from nzfleet import cli, config, plan, state, tick, world

from fakeworld import CODE, SHOT, World, Runner, sh

NOISE = "terminating connection due to administrator command"
FAILING = "test money::tests::adds ... FAILED"


def other(n):
    return f"crates/nzela/src/pr{n}.rs"


class Readiness(unittest.TestCase):
    def test_a_bad_live_limit_does_not_skip_the_tick(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"NZFLEET_ENV": os.path.join(home, "env")}), \
                mock.patch.object(config, "_ENVIRONMENT", frozenset()), \
                mock.patch.object(config, "MAX_CPU_PERCENT", config.MAX_CPU_PERCENT), \
                mock.patch.object(config, "CONFIG_PROBLEM", None), \
                mock.patch.object(tick, "read", side_effect=world.Failed("offline")) as read, \
                mock.patch.object(tick, "say"):
            with open(os.environ["NZFLEET_ENV"], "w") as env_file:
                env_file.write("NZFLEET_MAX_CPU_PERCENT=100000\n")
            tick.run()
        read.assert_called_once_with()

    def test_a_tick_refreshes_live_controls_before_reading_work(self):
        with mock.patch.object(config, "refresh") as refresh, \
                mock.patch.object(tick, "read", side_effect=world.Failed("offline")), \
                mock.patch.object(tick, "say"):
            tick.run()
        refresh.assert_called_once_with()

    def test_a_passing_pr_becomes_ready_and_is_listed(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            s = w.tick()
            self.assertEqual(s["batches"][next(iter(s["batches"]))]["status"], "green")
            self.assertEqual(w.ready, [1])
            self.assertIn("nzfleet land ", w.queue())

    def test_a_pr_that_has_not_passed_is_not_listed(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            self.assertEqual((w.ready, w.queue().count("nzfleet land")), ([], 0))

    def test_a_pr_is_checked_once_for_one_head(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.tick()
            self.assertEqual(w.runner.calls, [[1]])

    def test_a_merge_keeps_a_stacked_childs_pass(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"}, base="pr1")
            w.tick()
            self.assertEqual(w.runner.calls, [[1, 2]])
            w.merge_pr(1)
            s = w.tick()
            self.assertEqual(w.runner.calls, [[1, 2]])
            self.assertEqual(w.queued(), [2])
            self.assertIn("2", s["passed"])

    def test_a_pr_is_unlisted_when_main_takes_a_merge_its_batch_did_not_hold(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.open_pr(9, {other(9): "nine\n"})
            w.merge_pr(9)
            w.tick()
            self.assertEqual(w.queued(), [])


class Replays(unittest.TestCase):
    def test_replay_uses_and_records_one_immutable_target_sha(self):
        for parent, expected_target in ((7, "parent-sha"), (None, "main-sha")):
            with self.subTest(parent=parent):
                heads = []

                def head_of(number):
                    heads.append(number)
                    return "source-sha" if number == 8 else "parent-sha"

                with mock.patch.object(cli.world, "fetch"), \
                        mock.patch.object(cli.tick, "read", return_value=[]), \
                        mock.patch.object(cli.plan, "parents", return_value={8: parent}), \
                        mock.patch.object(cli.world, "head_of", side_effect=head_of), \
                        mock.patch.object(cli.world, "main_sha", return_value="main-sha") as main_sha, \
                        mock.patch("nzfleet.shape.replay", return_value=("replayed", "pushed-sha")) as replay, \
                        mock.patch.object(cli.tick, "settle_replay") as settle:
                    cli.replay(SimpleNamespace(pr=8, dry_run=False))

                self.assertEqual(replay.call_args.args[1], expected_target)
                self.assertEqual(settle.call_args.args[5], expected_target)
                self.assertEqual(heads.count(parent), 1 if parent is not None else 0)
                self.assertEqual(heads.count(8), 1)
                self.assertEqual(main_sha.call_count, 1 if parent is None else 0)

    def test_a_standalone_completed_replay_keeps_its_source_and_is_not_rewritten(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            source = w.head(1)
            pushed = "replayed-head"
            base = w.main_sha()
            tick.settle_replay(1, source, None, "replayed", pushed, base)
            pr = plan.Pr(1, pushed, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.subprocess, "Popen") as popen:
                tick.shape([pr], {}, False)
            popen.assert_not_called()
            self.assertEqual(state.read()["replays"]["1"], {"sha": source, "pushed": pushed, "base": base})

    def test_a_completed_replay_is_reconsidered_when_main_moves(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            source = w.head(1)
            old_base = w.main_sha()
            with state.change() as s:
                s["replays"]["1"] = {"sha": source, "pushed": source, "base": old_base}
            w.open_pr(9, {other(9): "nine\n"})
            w.merge_pr(9)
            moved_base = w.main_sha()
            pr = plan.Pr(1, source, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as popen:
                tick.shape([pr], {}, False)
            popen.assert_called_once()
            self.assertNotEqual(old_base, moved_base)

    def test_a_completed_replay_is_reconsidered_when_its_parent_moves(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"}, base="pr1")
            source = w.head(2)
            old_parent = w.head(1)
            with state.change() as s:
                s["replays"]["2"] = {"sha": source, "pushed": source, "base": old_parent}
            moved_parent = "new-parent-head"
            pr = plan.Pr(2, source, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.world, "head_of", return_value=moved_parent), \
                    mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as popen:
                tick.shape([pr], {2: 1}, False)
            popen.assert_called_once()

    def test_a_legacy_pushed_only_record_does_not_suppress_a_replay(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            source = w.head(1)
            with state.change() as s:
                s["replays"]["1"] = {"pushed": source}
            pr = plan.Pr(1, source, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as popen:
                tick.shape([pr], {}, False)
            popen.assert_called_once()

    def test_an_active_replay_for_the_source_head_is_left_running(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            source = w.head(1)
            with state.change() as s:
                s["replays"]["1"] = {"sha": source, "pid": 123}
            pr = plan.Pr(1, source, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.agents, "_alive", return_value=True), \
                    mock.patch.object(tick.subprocess, "Popen") as popen:
                tick.shape([pr], {}, False)
            popen.assert_not_called()

    def test_a_completed_replay_head_survives_same_change_pass_normalization(self):
        source, pushed = "source-head", "pushed-head"
        with state.change() as s:
            s["passed"]["1"] = {"sha": source, "main": "verified-main"}
            s["replays"]["1"] = {"sha": source, "pushed": pushed}
        raw = [{"number": 1, "headRefOid": pushed, "isDraft": False, "headRefName": "nzf-branch",
                "updatedAt": "2026-01-01T00:00:00Z", "mergeable": "MERGEABLE"}]
        with mock.patch.object(tick.world, "open_prs", return_value=raw), \
                mock.patch.object(tick.world, "fetch"), \
                mock.patch.object(tick.world, "head_of", return_value=pushed), \
                mock.patch.object(tick.world, "change_id", return_value="same-change"), \
                mock.patch.object(tick.world, "changed_files", return_value=[other(1)]), \
                mock.patch.object(tick.world, "areas", return_value={"rust"}), \
                mock.patch.object(tick.world, "has_merge_commits", return_value=False), \
                mock.patch.object(tick.plan, "check_areas", return_value=frozenset({"rust"})), \
                mock.patch.object(tick.plan, "is_conflicting", return_value=False):
            prs = tick.read()
        self.assertEqual(prs[0].sha, pushed)

    def test_a_new_head_is_not_replaced_by_same_change_pass_evidence(self):
        with state.change() as s:
            s["passed"]["1"] = {"sha": "passed-head", "main": "verified-main"}
        raw = [{"number": 1, "headRefOid": "new-head", "isDraft": False, "headRefName": "nzf-branch",
                "updatedAt": "2026-01-01T00:00:00Z", "mergeable": "MERGEABLE"}]
        with mock.patch.object(tick.world, "open_prs", return_value=raw), \
                mock.patch.object(tick.world, "fetch"), \
                mock.patch.object(tick.world, "head_of", return_value="new-head"), \
                mock.patch.object(tick.world, "change_id", return_value="same-change"), \
                mock.patch.object(tick.world, "changed_files", return_value=[other(1)]), \
                mock.patch.object(tick.world, "areas", return_value={"rust"}), \
                mock.patch.object(tick.world, "has_merge_commits", return_value=False), \
                mock.patch.object(tick.plan, "check_areas", return_value=frozenset({"rust"})), \
                mock.patch.object(tick.plan, "is_conflicting", return_value=False):
            prs = tick.read()
        self.assertEqual(prs[0].sha, "new-head")

    def test_a_moved_head_is_not_hidden_by_old_replay_completion(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            old, moved = w.head(1), "moved-head"
            with state.change() as s:
                s["replays"]["1"] = {"sha": old, "pushed": old}
            pr = plan.Pr(1, moved, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as popen:
                tick.shape([pr], {}, False)
            popen.assert_called_once()
            self.assertEqual(state.read()["replays"]["1"]["sha"], moved)

    def test_a_native_train_batch_holds_exact_assembled_source_heads_only_while_running(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            source = w.head(1)
            train_head = source
            with state.change() as s:
                s["train_prs"]["1299"] = {"head": train_head, "source_heads": {"1": source},
                                               "members": [[1, source]]}
                s["batches"]["native-train"] = {"status": "running", "pid": 777,
                                                   "members": [[1299, train_head]]}
            pr = plan.Pr(1, source, frozenset({"rust"}), False, True, False)
            train = plan.Pr(1299, train_head, frozenset({"rust"}), False, False, False)
            with mock.patch.object(tick.world, "is_ancestor", return_value=True), \
                    mock.patch.object(tick.agents, "_alive", return_value=True), \
                    mock.patch.object(tick.subprocess, "Popen") as popen:
                tick.shape([pr, train], {}, False)
            popen.assert_not_called()
            edited = "independently-edited-source"
            pr = plan.Pr(1, edited, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.agents, "_alive", return_value=True), \
                    mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as popen:
                tick.shape([pr, train], {}, False)
            popen.assert_called_once()
            with state.change() as s:
                s["batches"]["native-train"]["status"] = "green"
            with mock.patch.object(tick.agents, "_alive", return_value=True), \
                    mock.patch.object(tick.subprocess, "Popen", return_value=SimpleNamespace(pid=789)) as popen:
                tick.shape([plan.Pr(1, source, frozenset({"rust"}), False, True, False), train], {}, False)
            popen.assert_called_once()

    def test_a_conflicting_pr_is_replayed_onto_main(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.advance_main({"docs/other.md": "main moved\n"})
            w.pinned[1] = (w.head(1), "CONFLICTING")
            before = w.head(1)
            w.tick()
            self.assertNotEqual(w.head(1), before)
            self.assertIn("#1 replayed onto main", w.said)
            self.assertEqual(state.read()["hands"], {})

    def test_a_registered_train_is_never_replayed_by_automatic_shape(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            before = w.head(1)
            for reviewed in ("", before):
                for conflict, merge_commits in ((True, False), (False, True)):
                    with self.subTest(
                        reviewed=bool(reviewed), conflict=conflict, merge_commits=merge_commits
                    ):
                        with state.change() as s:
                            s["train_prs"]["1"] = {
                                "branch": "train/1", "members": [1], "left_out": [], "dropped": [],
                                "compiled": True, "reviewed": reviewed, "mailing": False, "mailed": False,
                            }
                        pr = plan.Pr(1, before, frozenset({"rust"}), False, conflict, merge_commits)
                        with mock.patch.object(
                            tick.subprocess, "Popen", return_value=SimpleNamespace(pid=123)
                        ) as popen:
                            tick.shape([pr], {}, False)
                        popen.assert_not_called()
                        self.assertEqual(w.head(1), before)
                        self.assertIn("1", state.read()["train_prs"])

    def test_an_open_train_holds_its_exact_member_head_during_shape(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            head = w.head(1)
            with state.change() as s:
                s["trains"]["frozen"] = {"id": "frozen", "base": w.main_sha(), "members": [[1, head]],
                                           "tree": "tree", "status": "open", "why": ""}
            pr = plan.Pr(1, head, frozenset({"rust"}), False, True, False)
            with mock.patch.object(tick.subprocess, "Popen") as popen:
                tick.shape([pr], {}, False)
            popen.assert_not_called()

    def test_two_branches_that_redraw_one_screenshot_both_land_without_hands(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n", SHOT: "one's\n"})
            w.open_pr(2, {other(2): "two\n", SHOT: "two's\n"})
            w.merge_pr(1)
            s = w.tick()
            self.assertEqual((s["hands"], s["agents"]), ({}, {}))
            self.assertIn("#2 replayed onto main", w.said)
            self.assertEqual(sh(w.origin, "git", "show", f"{w.head(2)}:{SHOT}"), "one's")
            w.tick()
            self.assertEqual(w.ready, [2])

    def test_a_conflict_in_code_is_left_for_hands(self):
        with World() as w:
            w.open_pr(1, {CODE: "mine\n"})
            w.advance_main({CODE: "main's\n"})
            before = w.head(1)
            s = w.tick()
            self.assertEqual(s["hands"]["1"]["files"], [CODE])
            self.assertEqual(w.head(1), before)
            self.assertIn(f"#1 needs hands: {CODE}", w.said)


class RedBatches(unittest.TestCase):
    def three(self, w, logs):
        w.runner = Runner(logs=logs)
        for n in (1, 2, 3):
            w.open_pr(n, {other(n): f"{n}\n"})

    def test_a_red_batch_bisects_to_the_culprit(self):
        with World() as w:
            self.three(w, {2: FAILING})
            s = w.tick()
            self.assertEqual(set(s["failed"]), {"2"})
            self.assertEqual(s["failed"]["2"]["tests"], ["money::tests::adds"])
            self.assertEqual(w.ready, [1, 3])
            self.assertEqual(w.queued(), [1, 3])

    def test_a_culprit_is_not_checked_again_until_it_moves(self):
        with World() as w:
            self.three(w, {2: FAILING})
            w.tick()
            calls = len(w.runner.calls)
            w.tick()
            self.assertEqual(len(w.runner.calls), calls)

    def test_a_counterexample_stops_the_batch_without_a_culprit(self):
        with World() as w:
            w.runner = Runner(flaky=f"{FAILING}\nminimal failing input: (1, 2)")
            for n in (1, 2):
                w.open_pr(n, {other(n): f"{n}\n"})
            s = w.tick()
            batch = next(iter(s["batches"].values()))
            self.assertEqual((batch["status"], s["failed"], s["passed"], w.ready), ("counterexample", {}, {}, []))
            self.assertEqual(len(w.runner.calls), 2)

    def test_a_failing_test_beside_machine_noise_is_code(self):
        with World() as w:
            self.three(w, {2: f"{FAILING}\n{NOISE}"})
            s = w.tick()
            self.assertEqual(set(s["failed"]), {"2"})

    def test_machine_noise_alone_blames_nobody(self):
        with World() as w:
            self.three(w, {2: NOISE})
            s = w.tick()
            self.assertEqual((s["failed"], s["passed"], w.ready), ({}, {}, []))
            self.assertEqual({b["status"] for b in s["batches"].values()}, {"infrastructure"})


class MainHealth(unittest.TestCase):
    def red_main(self, w, log):
        w.ci = ("completed", {"rust": "failure", "console": "success"}, 70)
        w.logs["rust"] = log
        w.tick()
        return [line for line in w.said if "is red" in line]

    def test_a_failing_test_beside_machine_noise_makes_main_red_in_code(self):
        with World() as w:
            red = self.red_main(w, f"{FAILING}\n{NOISE}")
            self.assertEqual(len(red), 1)
            self.assertIn("rust: money::tests::adds", red[0])

    def test_machine_noise_alone_leaves_main_unreported(self):
        with World() as w:
            self.assertEqual(self.red_main(w, NOISE), [])


class Deploys(unittest.TestCase):
    def run_of(self, w, run_id, sha, status="completed", conclusion="success", gate="success"):
        w.deploys.insert(0, {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion})
        w.deploy_jobs[run_id] = {"Confirm successful main push": gate, "Deploy API and background services": conclusion,
                                 "Deploy admin console": "success", "Deploy website and signed Android app": "success"}

    def reported(self, w):
        return [line for line in w.said if line.startswith("deployed ")]

    def test_a_deploy_is_reported_once_its_gate_and_jobs_have_passed(self):
        with World() as w:
            self.run_of(w, 5, w.main_sha())
            w.tick()
            w.tick()
            self.assertEqual(len(self.reported(w)), 1)
            self.assertEqual(w.comments[0].split(":")[0], f"Deployed `{w.main_sha()[:8]}`")

    def test_a_run_still_going_is_not_reported_until_it_completes(self):
        with World() as w:
            self.run_of(w, 5, w.main_sha(), status="in_progress", conclusion="")
            w.tick()
            self.assertEqual(self.reported(w), [])
            w.deploys[0].update(status="completed", conclusion="success")
            w.deploy_jobs[5]["deploy"] = "success"
            w.tick()
            self.assertEqual(len(self.reported(w)), 1)

    def test_a_run_whose_gate_did_not_pass_is_not_reported(self):
        with World() as w:
            self.run_of(w, 5, w.main_sha(), gate="skipped")
            w.tick()
            self.assertEqual(self.reported(w), [])

    def test_a_run_for_a_commit_not_on_main_is_not_reported(self):
        with World() as w:
            stray = w.open_pr(1, {other(1): "one\n"})
            self.run_of(w, 5, stray)
            w.tick()
            self.assertEqual(self.reported(w), [])

    def test_a_failed_job_is_named(self):
        with World() as w:
            self.run_of(w, 5, w.main_sha(), conclusion="failure")
            w.deploy_jobs[5]["console"] = "failure"
            w.tick()
            self.assertTrue(any("deploy of" in line and "console" in line for line in w.said))


if __name__ == "__main__":
    unittest.main()
