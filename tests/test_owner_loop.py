import unittest
from unittest import mock

from nzfleet import agents, cli, owner, state, tick, world

from fakeworld import World, Runner

FAILING = "test money::tests::adds ... FAILED"
NO_SPACE = "No space left on device"


def other(n):
    return f"crates/nzela/src/pr{n}.rs"


def always_red(name, areas, members):
    numbers = [n for n, _ in members]
    return Runner.result(1, numbers, FAILING)


class ReadySets(unittest.TestCase):
    def test_a_ready_set_is_emailed_with_what_each_pr_does_and_the_merge_loop(self):
        with World() as w:
            w.texts[1] = ("Hold a rate for a minute", "A quote keeps its rate.\n\nThe second paragraph is left out.")
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            (subject, body), = w.mailed("ready")
            self.assertIn("#1 Hold a rate for a minute", body)
            self.assertIn("A quote keeps its rate.", body)
            self.assertNotIn("second paragraph", body)
            self.assertRegex(body, r"nzfleet land [0-9a-f]{8}")

    def test_the_merge_loop_is_in_stack_order(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"}, base="pr1")
            w.tick()
            train, = w.tick()["trains"].values()
            self.assertEqual([n for n, _ in train["members"]], [1, 2])
            self.assertIn(f"nzfleet land {train['id']}", w.mailed("ready")[0][1])

    def test_the_same_set_is_never_emailed_twice(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.tick()
            w.tick()
            self.assertEqual(len(w.mailed("ready")), 1)

    def test_a_merge_outside_a_train_mails_the_new_train_for_what_is_left(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"}, base="pr1")
            w.tick()
            w.merge_pr(1)
            w.tick()
            self.assertEqual(len(w.mailed("ready")), 2)
            self.assertEqual(w.queued(), [2])
            self.assertIn(w.queue().split("nzfleet land ")[-1].split("\n")[0], w.mailed("ready")[1][1])

    def test_a_set_with_a_new_pr_is_emailed_again(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.open_pr(2, {other(2): "two\n"})
            w.tick()
            self.assertEqual(len(w.mailed("ready")), 2)
            self.assertIn("nzfleet land", w.mailed("ready")[1][1])
            self.assertEqual(w.queued(), [1, 2])

    def test_a_mail_that_did_not_go_is_sent_on_the_next_tick(self):
        with World() as w:
            w.mail_ok = False
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.mail_ok = True
            w.tick()
            self.assertEqual(len(w.mailed("ready")), 1)

    def test_a_pr_that_has_not_passed_is_not_emailed(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            self.assertEqual(w.mailed("ready"), [])

    def test_the_prs_of_a_passing_batch_are_marked_ready_for_review(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"}, base="pr1")
            w.tick()
            self.assertEqual(w.ready, [1, 2])


class Events(unittest.TestCase):
    def test_a_merge_is_an_event_and_in_the_digest(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.merge_pr(1)
            s = w.tick()
            self.assertTrue(any("main moved" in line and "#1" in line for line in w.said))
            self.assertTrue(any("#1" in line for _, line in s["digest"]))

    def test_a_deploy_that_starts_is_reported_once(self):
        with World() as w:
            w.deploys.append({"databaseId": 8, "headSha": w.main_sha(), "status": "in_progress", "conclusion": ""})
            w.deploy_jobs[8] = {"Confirm successful main push": "success", "Deploy API and background services": None}
            w.tick()
            w.tick()
            started = [line for line in w.said if "started" in line]
            self.assertEqual(len(started), 1)
            self.assertIn(w.main_sha()[:8], started[0])

    def test_a_deploy_that_succeeds_or_fails_is_in_the_digest(self):
        with World() as w:
            w.deploys.append({"databaseId": 8, "headSha": w.main_sha(), "status": "completed", "conclusion": "failure"})
            w.deploy_jobs[8] = {"Confirm successful main push": "success", "Deploy API and background services": "failure"}
            s = w.tick()
            self.assertTrue(any("failed" in line for _, line in s["digest"]))

    def test_a_red_batch_names_its_area_and_test(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"})
            w.tick()
            red = [line for line in w.said if "is red" in line]
            self.assertEqual(len(red), 1)
            self.assertIn("rust", red[0])
            self.assertIn("money::tests::adds", red[0])
            w.tick()
            self.assertEqual(len([line for line in w.said if "is red" in line]), 1)

    def test_batches_that_were_red_long_ago_are_not_news(self):
        with World() as w:
            with state.change() as s:
                s["batches"]["old"] = {"group": "core", "members": [], "areas": ["rust"], "main": "m", "started": 1.0,
                                       "finished": 2.0, "status": "red", "pid": 0, "line": FAILING}
            w.tick()
            self.assertEqual([line for line in w.said if "is red" in line], [])

    def merged_pr_and_a_day_of_news(self, w):
        with state.change() as s:
            s["digest_sent"] = 1.0
        w.open_pr(1, {other(1): "one\n"})
        w.tick()
        w.merge_pr(1)
        w.tick()

    def test_events_go_out_in_one_digest_a_day(self):
        with World() as w:
            self.merged_pr_and_a_day_of_news(w)
            w.tick()
            (subject, body), = w.mailed("events")
            self.assertIn("main moved", body)
            self.assertEqual(state.read()["digest"], [])

    def test_the_digest_waits_a_day_after_it_went_out(self):
        with World() as w:
            self.merged_pr_and_a_day_of_news(w)
            w.advance_main({"docs/a.md": "a\n"})
            w.tick()
            self.assertEqual(len(w.mailed("events")), 1)
            self.assertEqual(len(state.read()["digest"]), 1)

    def test_the_first_digest_comes_a_day_after_the_first_event(self):
        with World() as w:
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.merge_pr(1)
            s = w.tick()
            self.assertEqual((w.mailed("events"), len(s["digest"]) > 0), ([], True))


class PeopleDecide(unittest.TestCase):
    def chain(self, w, first):
        with state.change() as s:
            s["todo"] = [{"key": "a1", "task": "x", "area": "gcloud"}, {"key": "a2", "task": "y", "area": "gcloud"}]
            s["agents"] = {"a1": {"status": "done", **first}}

    def test_a_chain_waiting_on_a_person_emails_the_decision_at_once_and_only_once(self):
        with World() as w:
            self.chain(w, {"pr": None, "stated_no_pr": True})
            s = w.tick()
            w.tick()
            (subject, body), = w.mailed("needs you")
            self.assertEqual(body, "a1 stated `PR: none`; a person decides (nzfleet attach a1 <pr>)")
            self.assertTrue(any("a1 stated" in line for _, line in s["digest"]))

    def test_a_chain_that_is_only_waiting_does_not_email(self):
        with World() as w:
            self.chain(w, {"status": "running"})
            with state.change() as s:
                s["agents"]["a1"]["status"] = "running"
            w.tick()
            self.assertEqual(w.mailed("needs you"), [])

    def test_a_decision_that_is_made_and_comes_back_is_emailed_again(self):
        with World() as w:
            self.chain(w, {"pr": None, "stated_no_pr": True})
            w.tick()
            with state.change() as s:
                s["agents"]["a1"]["pr"] = {"number": 4, "branch": "b", "how": "attached"}
                s["agents"]["a1"].pop("stated_no_pr")
            w.open_pr(4, {other(4): "four\n"})
            w.tick()
            with state.change() as s:
                s["agents"]["a1"] = {"status": "done", "pr": None, "stated_no_pr": True}
            w.tick()
            self.assertEqual(len(w.mailed("needs you")), 2)


class FailuresOutsideTheCode(unittest.TestCase):
    def test_a_failure_the_code_did_not_cause_runs_once_more_then_waits_for_a_person(self):
        with World() as w:
            w.runner = Runner(logs={1: NO_SPACE})
            w.open_pr(1, {other(1): "one\n"})
            for _ in range(4):
                w.tick()
            self.assertEqual(w.runner.calls, [[1], [1]])
            subjects = [m[0] for m in w.mailed("outside its code")]
            self.assertEqual(len(subjects), 2)
            self.assertIn("trying once more", subjects[0])
            self.assertIn("twice", subjects[1])

    def test_a_check_that_fails_on_main_alone_is_not_the_prs_fault_and_is_retried_once(self):
        with World() as w:
            calls = []
            w.runner = lambda *args: calls.append(args) or always_red(*args)
            w.open_pr(1, {other(1): "one\n"})
            w.open_pr(2, {other(2): "two\n"})
            s = w.tick()
            self.assertEqual(next(iter(s["batches"].values()))["status"], "main-broken")
            self.assertEqual(s["failed"], {})
            self.assertIn([], [[n for n, _ in members] for _, _, members in calls])
            once = len(calls)
            w.tick()
            w.tick()
            self.assertEqual(len(calls), 2 * once)
            self.assertEqual(len(w.mailed("outside its code")), 4)

    def test_a_single_failed_head_stays_red_without_a_main_probe_or_automatic_rerun(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            head = w.open_pr(1, {other(1): "one\n"})
            for _ in range(3):
                s = w.tick()
            batch, = s["batches"].values()
            self.assertEqual(batch["status"], "red")
            self.assertEqual(s["failed"]["1"]["sha"], head)
            self.assertEqual(s["untied"], {})
            self.assertEqual(w.runner.calls, [[1]])
            self.assertEqual(w.mailed("outside its code"), [])

    def test_a_lost_batch_counts_and_the_second_loss_waits_for_a_person(self):
        with World() as w:
            head = w.open_pr(1, {other(1): "one\n"})
            with state.change() as s:
                s["untied"]["1"] = {"sha": head, "times": 1, "told": 1, "why": "earlier"}
                s["batches"]["core-1"] = {"group": "core", "members": [[1, head]], "areas": ["rust"], "main": "m",
                                          "started": 1.0, "status": "running", "pid": 2 ** 22 + 5}
            s = w.tick()
            self.assertEqual((s["batches"]["core-1"]["status"], w.runner.calls), ("lost", []))
            self.assertEqual(len(w.mailed("twice")), 1)

    def test_a_new_push_starts_again(self):
        with World() as w:
            w.runner = Runner(logs={1: NO_SPACE})
            w.open_pr(1, {other(1): "one\n"})
            w.tick()
            w.tick()
            w.tick()
            w.runner = Runner()
            w.open_pr(1, {other(1): "one, changed\n"})
            w.tick()
            self.assertEqual(w.runner.calls, [[1]])

    def test_a_code_failure_is_not_counted_as_outside_the_code(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            w.open_pr(1, {other(1): "one\n"})
            s = w.tick()
            self.assertEqual((s["untied"], w.mailed("outside its code")), ({}, []))


class ARunsMerge(unittest.TestCase):
    def test_a_merge_seen_starts_the_waiting_item_in_the_same_tick(self):
        with World() as w:
            w.runner = Runner(logs={1: FAILING})
            w.open_pr(1, {other(1): "one\n"})
            with state.change() as s:
                s["todo"] = [{"key": "a1", "task": "x", "area": "ar"}, {"key": "a2", "task": "y", "area": "ar"}]
                s["agents"] = {"a1": {"status": "done", "pr": {"number": 1, "branch": "pr1", "how": "stated"}, "area": "ar"}}
            started = []
            with mock.patch.object(tick, "work", w.real_work), mock.patch.object(tick, "fix_failures"), \
                    mock.patch.object(agents, "start", side_effect=lambda *a, **k: started.append(a) or "started"):
                w.tick()
                self.assertEqual(started, [])
                w.merge_pr(1)
                w.tick()
            self.assertEqual([a[0] for a in started], ["a2"])
            self.assertIn("Branch from upstream/main", started[0][1])

    def test_an_item_after_one_that_found_nothing_to_change_starts_on_main(self):
        with World() as w:
            with state.change() as s:
                s["todo"] = [{"key": "a1", "task": "x", "area": "ar"}, {"key": "a2", "task": "y", "area": "ar"}]
                s["agents"] = {"a1": {"status": "done", "pr": None, "area": "ar"}}
            started = []
            with mock.patch.object(tick, "work", w.real_work), \
                    mock.patch.object(agents, "start", side_effect=lambda *a, **k: started.append(a) or "started"):
                w.tick()
            self.assertEqual([a[0] for a in started], ["a2"])
            self.assertIn("Branch from upstream/main", started[0][1])

    def test_main_moving_makes_the_daemon_tick_at_once(self):
        ticks = []
        with mock.patch.object(tick, "watch", return_value=False), mock.patch.object(world, "main_remote", return_value="b"), \
                mock.patch.object(tick, "say"):
            last, main = cli.poll(1000.0, "a", 1015.0, run=lambda: ticks.append(1))
        self.assertEqual((ticks, last, main), ([1], 1015.0, "b"))

    def test_a_quiet_main_leaves_the_tick_to_the_minute(self):
        ticks = []
        with mock.patch.object(tick, "watch", return_value=False), mock.patch.object(world, "main_remote", return_value="a"):
            cli.poll(1000.0, "a", 1015.0, run=lambda: ticks.append(1))
            cli.poll(1000.0, "a", 1060.0, run=lambda: ticks.append(2))
        self.assertEqual(ticks, [2])

    def test_a_feed_that_fails_does_not_hide_a_merge(self):
        ticks = []
        with mock.patch.object(tick, "watch", side_effect=world.Failed("feed")), \
                mock.patch.object(world, "main_remote", return_value="b"), mock.patch.object(tick, "say"):
            cli.poll(1000.0, "a", 1015.0, run=lambda: ticks.append(1))
        self.assertEqual(ticks, [1])

    def test_the_first_look_at_main_is_not_a_merge(self):
        self.assertFalse(owner.main_moved("", "a"))


if __name__ == "__main__":
    unittest.main()
