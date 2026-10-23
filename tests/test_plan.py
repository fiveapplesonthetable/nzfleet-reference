import os
import unittest

from nzfleet import plan, rebases, task_queue
from nzfleet.plan import Bisect, Passed, Pr


def pr(n, sha=None, areas=("rust",), **kw):
    return Pr(n, sha or f"s{n}", frozenset(areas), **kw)


class Groups(unittest.TestCase):
    def test_android_only_is_its_own_group(self):
        self.assertEqual(plan.group_of(frozenset({"android", "secrets"})), "android")

    def test_console_and_site_share_a_group(self):
        self.assertEqual(plan.group_of(frozenset({"console", "site"})), "console")

    def test_anything_touching_rust_is_core(self):
        self.assertEqual(plan.group_of(frozenset({"console", "rust"})), "core")

    def test_a_pr_touching_every_area_is_checked_apart(self):
        self.assertEqual(plan.group_of(frozenset({"android", "console", "rust", "image", "deploy"})), "wide")

    def test_docs_and_scripts_take_the_fast_lane(self):
        self.assertEqual(plan.group_of(frozenset({"secrets"})), "light")
        self.assertEqual(plan.group_of(frozenset({"secrets", "deploy", "site"})), "light")
        self.assertEqual(plan.group_of(frozenset({"deploy", "rust"})), "core")


class Stacks(unittest.TestCase):
    def test_a_child_names_its_nearest_parent(self):
        chain = {("s1", "s2"), ("s1", "s3"), ("s2", "s3")}
        up = plan.parents([pr(1), pr(2), pr(3)], lambda a, b: (a, b) in chain)
        self.assertEqual(up, {2: 1, 3: 2})

    def test_parents_come_before_children(self):
        self.assertEqual(plan.ordered([3, 1, 2], {3: 1, 1: 2}), [2, 1, 3])

    def test_a_child_rides_in_its_parents_group(self):
        waiting = [pr(1, areas=("rust",)), pr(2, areas=("console",))]
        out = plan.batches(waiting, {2: 1}, set(), 2)
        self.assertEqual(out, [("core", [1, 2], ["console", "rust"])])


class Cap(unittest.TestCase):
    def test_a_batch_holds_at_most_six(self):
        self.assertEqual(plan.cap(list(range(1, 10)), {}), [1, 2, 3, 4, 5, 6])

    def test_a_child_rides_with_its_parent_even_past_six(self):
        self.assertEqual(plan.cap([1, 2, 3, 4, 5, 6, 7], {7: 6}), [1, 2, 3, 4, 5, 6, 7])

    def test_a_planned_batch_is_capped(self):
        out = plan.batches([pr(n) for n in range(1, 10)], {}, set(), 1)
        self.assertEqual(out[0][1], [1, 2, 3, 4, 5, 6])

    def test_a_child_whose_parent_was_left_out_waits(self):
        self.assertEqual(plan.cap([1, 2, 3, 4, 5, 6, 7, 8], {8: 7}), [1, 2, 3, 4, 5, 6])


class Batches(unittest.TestCase):
    def test_one_batch_per_group(self):
        waiting = [pr(1), pr(2, areas=("android",)), pr(3)]
        groups = [g for g, _, _ in plan.batches(waiting, {}, set(), 3)]
        self.assertEqual(groups, ["core", "android"])

    def test_a_running_group_gets_no_second_batch(self):
        out = plan.batches([pr(1), pr(2, areas=("android",))], {}, {"core"}, 3)
        self.assertEqual([g for g, _, _ in out], ["android"])

    def test_no_more_than_the_free_slots(self):
        waiting = [pr(1), pr(2, areas=("android",)), pr(3, areas=("console",))]
        self.assertEqual(len(plan.batches(waiting, {}, set(), 1)), 1)

    def test_a_head_that_passed_is_not_checked_again(self):
        passed = Passed("s1", "m", [1], "b")
        self.assertFalse(plan.needs_check(pr(1), passed, None, ["rust"]))

    def test_a_moved_head_is_checked_again(self):
        passed = Passed("old", "m", [1], "b")
        self.assertTrue(plan.needs_check(pr(1), passed, None))

    def test_a_second_batch_in_the_same_second_keeps_its_own_record(self):
        taken = {"core-100": {}, "core-100-1": {}}
        self.assertEqual(plan.new_batch_id("core", 100.7, taken), "core-100-2")

    def test_a_head_that_failed_is_not_retried(self):
        self.assertFalse(plan.needs_check(pr(1), None, "s1"))


class Bisection(unittest.TestCase):
    def run_to_end(self, members, broken):
        """Probes until a verdict, where a prefix fails if it holds any broken member."""
        b = Bisect(members, {len(members): False})
        probes = 0
        while True:
            step, value = plan.next_probe(b)
            if step != "probe":
                return step, value, probes
            probes += 1
            b.results[value] = not (set(members[:value]) & broken)

    def test_finds_the_first_broken_member(self):
        for culprit in range(8):
            members = list(range(100, 108))
            step, value, _ = self.run_to_end(members, {members[culprit]})
            self.assertEqual((step, value), ("culprit", members[culprit]))

    def test_with_two_broken_it_names_the_earlier(self):
        members = [1, 2, 3, 4, 5]
        self.assertEqual(self.run_to_end(members, {2, 4})[:2], ("culprit", 2))

    def test_takes_logarithmic_probes(self):
        members = list(range(16))
        self.assertLessEqual(self.run_to_end(members, {13})[2], 5)

    def test_main_failing_on_its_own_blames_no_member(self):
        b = Bisect([1, 2], {2: False, 1: False, 0: False})
        self.assertEqual(plan.next_probe(b), ("main-broken", None))

    def test_a_single_member_batch_checks_main_before_blaming(self):
        b = Bisect([7], {1: False})
        self.assertEqual(plan.next_probe(b), ("probe", 0))
        b.results[0] = True
        self.assertEqual(plan.next_probe(b), ("culprit", 7))


class Ready(unittest.TestCase):
    passed = Passed("s3", "m0", [1, 2, 3], "b")

    def test_holds_while_main_has_not_moved(self):
        self.assertTrue(plan.still_ready(pr(3), self.passed, []))

    def test_holds_when_main_took_earlier_members(self):
        self.assertTrue(plan.still_ready(pr(3), self.passed, [1, 2]))

    def test_lapses_when_main_took_something_else(self):
        self.assertFalse(plan.still_ready(pr(3), self.passed, [1, 99]))

    def test_lapses_when_main_took_a_later_member(self):
        passed = Passed("s1", "m0", [1, 2, 3], "b")
        self.assertFalse(plan.still_ready(pr(1), passed, [2]))

    def test_lapses_when_main_history_cannot_be_read(self):
        self.assertFalse(plan.still_ready(pr(3), self.passed, None))

    def test_lapses_when_the_head_moved(self):
        self.assertFalse(plan.still_ready(pr(3, sha="new"), self.passed, []))

    def test_lapses_when_github_says_it_conflicts(self):
        self.assertFalse(plan.still_ready(pr(3, conflicting=True), self.passed, []))

    def test_queue_follows_batch_start_then_place(self):
        ready = {5: Passed("a", "m", [5, 6], "late"), 6: Passed("b", "m", [5, 6], "late"),
                 9: Passed("c", "m", [9], "early")}
        self.assertEqual(plan.queue(ready, {"early": 1, "late": 2}), [9, 5, 6])

    def test_the_owner_hears_only_when_the_list_grows(self):
        self.assertFalse(plan.grew([1, 2], [2]))
        self.assertTrue(plan.grew([1], [1, 3]))

    def test_the_one_command_lands_a_train_by_its_id(self):
        self.assertEqual(plan.land_command("a1b2c3d4"), "nzfleet land a1b2c3d4")
        self.assertEqual(plan.land_command(""), "")


class Covered(unittest.TestCase):
    def test_a_pass_over_fewer_areas_than_the_pr_touches_does_not_count(self):
        self.assertFalse(plan.covered(frozenset({"android", "secrets"}), ["secrets"]))
        self.assertTrue(plan.covered(frozenset({"android", "secrets"}), ["android", "rust", "secrets"]))


class Skipped(unittest.TestCase):
    def test_an_area_left_to_main_ci_does_not_block_a_pass(self):
        self.assertTrue(plan.covered(frozenset({"rust", "formal"}), ["rust"], frozenset({"formal"})))
        self.assertFalse(plan.covered(frozenset({"rust", "formal"}), ["rust"]))

    def test_formal_sources_are_left_to_post_merge_checks(self):
        files = [".github/workflows/red.yaml", "formal/tla/Lifecycle.tla", "formal/kani/run.sh"]
        selected = frozenset({"deploy", "formal", "secrets"})
        self.assertEqual(plan.check_areas(selected, files, frozenset({"formal"})), selected - {"formal"})

    def test_ordinary_code_changes_keep_formal_post_submit(self):
        selected = frozenset({"rust", "fuzz", "e2e", "formal", "secrets"})
        self.assertEqual(plan.check_areas(selected, ["crates/nzela-core/src/lib.rs"], frozenset({"formal"})),
                         selected - {"formal"})

    def test_formal_inputs_never_put_formal_on_a_train(self):
        files = [".github/workflows/formal.yaml", "formal/kani/run.sh", "formal/tla/run.sh"]
        selected = frozenset({"deploy", "formal", "secrets"})
        self.assertEqual(plan.check_areas(selected, files, frozenset(), train=True),
                         (selected - {"formal"}) | {"compile"})
    def test_formal_workflow_changes_do_not_add_a_train_check(self):
        self.assertEqual(plan.check_areas(frozenset({"deploy", "secrets", "formal"}),
                                          [".github/workflows/formal.yaml"], frozenset()),
                         frozenset({"deploy", "secrets"}))

    def test_ci_and_deploy_workflow_names_do_not_require_formal(self):
        files = [".github/workflows/ci.yaml", ".github/workflows/deploy.yaml"]
        selected = frozenset({"deploy", "secrets"})
        self.assertEqual(plan.check_areas(selected, files, frozenset({"formal"})), selected)

    def test_tla_sources_add_the_formal_check(self):
        selected = frozenset({"secrets"})
        self.assertEqual(plan.check_areas(selected, ["formal/tla/Lifecycle.tla"], frozenset()),
                         frozenset({"formal", "secrets"}))


class TheCompileGate(unittest.TestCase):
    def test_formal_area_keeps_tla_and_kani_checks(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "areas", "formal.sh")) as f:
            script = f.read()
        body = script[script.index("check_formal() {"):]
        self.assertIn("make tla", body)
        self.assertIn("make kani", body)

    def test_a_train_compiles_every_client(self):
        core = frozenset({"rust", "fuzz", "secrets"})
        areas = plan.check_areas(core, ["crates/nzela-core/src/lib.rs"], frozenset(), train=True)
        self.assertIn(plan.GATE, areas)

    def test_one_change_compiles_only_what_changed_asked_for(self):
        self.assertNotIn(plan.GATE, plan.check_areas(frozenset({"console", "secrets"}), ["frontend/admin/src/App.tsx"], frozenset()))

    def test_the_gate_builds_the_server_android_and_the_console(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "areas", "compile.sh")) as f:
            script = f.read()
        body = script[script.index("compile_rust() {"):script.index("check_compile() {")]
        for step in ("--features nzela/fuzzing", ":app:compileDebugKotlin", ":app:compileReleaseKotlin",
                     " compileDebugUnitTestKotlin", ":core:model:compileTestKotlin", "typecheck"):
            self.assertIn(step, body)


class Events(unittest.TestCase):
    def test_a_merge_says_merged(self):
        e = {"type": "PullRequestEvent", "payload": {"action": "closed", "pull_request": {"number": 812, "merged": True, "title": "Fix"}}}
        self.assertEqual(plan.describe(e), "merged #812 Fix")

    def test_a_closed_unmerged_pr_says_closed(self):
        e = {"type": "PullRequestEvent", "payload": {"action": "closed", "pull_request": {"number": 9, "merged": False, "title": "X"}}}
        self.assertEqual(plan.describe(e), "closed #9 X")

    def test_a_comment_names_who_and_where(self):
        e = {"type": "IssueCommentEvent", "actor": {"login": "owner"},
             "payload": {"action": "created", "issue": {"number": 5, "pull_request": {}}, "comment": {"body": "please fix"}}}
        self.assertEqual(plan.describe(e), "owner commented on PR #5: please fix")

    def test_a_long_comment_is_one_line(self):
        e = {"type": "IssueCommentEvent", "actor": {"login": "a"},
             "payload": {"action": "created", "issue": {"number": 1}, "comment": {"body": "line one\nline two " + "x" * 300}}}
        line = plan.describe(e)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 160)

    def test_noise_is_dropped(self):
        self.assertIsNone(plan.describe({"type": "WatchEvent", "payload": {}}))


class Reruns(unittest.TestCase):
    def test_only_the_failed_areas_run_again(self):
        self.assertEqual(plan.failed_areas("failed: console e2e", ["rust", "e2e", "console", "formal"]), ["e2e", "console"])

    def test_a_summary_that_names_nothing_reruns_everything(self):
        self.assertEqual(plan.failed_areas("merge-check left no result", ["rust", "formal"]), ["rust", "formal"])


class Limits(unittest.TestCase):
    def test_batches_and_agents_run_in_slices_of_their_own(self):
        from unittest import mock
        from nzfleet import machine
        # A ci container has no user manager, where scoped() rightly leaves the command alone.
        with mock.patch("shutil.which", return_value="/usr/bin/systemd-run"), mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            slices = {kind: next(a for a in machine.scoped(kind, ["x"]) if a.startswith("--slice=")) for kind in machine.LIMITS}
        self.assertEqual(len(set(slices.values())), 2)

    def test_the_slice_quotas_and_the_runners_fit_in_the_machine(self):
        import os
        from pathlib import Path
        bin_ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
        quota = lambda path: int(next(l for l in Path(bin_, path).read_text().splitlines() if l.startswith("CPUQuota=")).strip()[9:-1]) // 100
        ci = quota("root/user-1001.slice.conf") + quota("root/user-1002.slice.conf")
        self.assertLessEqual(ci + quota("root/user-1000.slice.conf") + 4, 72)
        self.assertLessEqual(max(quota("nzfleet-batch.slice"), quota("nzfleet-agent.slice")), quota("root/user-1000.slice.conf"))

    def test_agents_yield_the_cpu_to_batches_when_both_want_it(self):
        import os
        from pathlib import Path
        bin_ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
        weight = lambda name: int(next(l for l in Path(bin_, name).read_text().splitlines() if l.startswith("CPUWeight=")).split("=")[1])
        self.assertEqual((weight("nzfleet-batch.slice"), weight("nzfleet-agent.slice")), (100, 20))

    def test_a_scoped_command_still_ends_with_the_command(self):
        from nzfleet import machine
        argv = machine.scoped("agent", ["echo", "hi"])
        self.assertEqual(argv[-2:], ["echo", "hi"])


class Work(unittest.TestCase):
    todo = [{"key": "a", "task": "x"}, {"key": "b", "task": "y", "after": ["a"]}, {"key": "c", "task": "z"}]

    def test_the_oldest_ready_task_starts_first(self):
        self.assertEqual(plan.next_work(self.todo, {}, 0, 4, False)["key"], "a")

    def test_a_task_waits_for_its_prerequisite(self):
        agents = {"a": {"status": "running"}}
        self.assertEqual(plan.next_work(self.todo, agents, 1, 4, False)["key"], "c")
        agents = {"a": {"status": "done"}, "c": {"status": "running"}}
        self.assertEqual(plan.next_work(self.todo, agents, 1, 4, False)["key"], "b")

    def test_nothing_starts_without_room(self):
        self.assertIsNone(plan.next_work(self.todo, {}, 4, 4, False))
        self.assertIsNone(plan.next_work(self.todo, {}, 0, 4, True))

    def test_a_started_task_is_not_started_again(self):
        agents = {k: {"status": "done"} for k in "abc"}
        self.assertIsNone(plan.next_work(self.todo, agents, 0, 4, False))

    def test_finished_tasks_leave_todo_and_keep_the_area_head(self):
        todo = [{"key": "finished", "area": "rust", "task": "done"}, {"key": "pending", "area": "rust", "task": "work"}]
        remaining, history, heads = task_queue.compact(todo, {"finished": {"status": "done"}}, {}, {})
        self.assertEqual([item["key"] for item in remaining], ["pending"])
        self.assertEqual(history["finished"]["status"], "done")
        self.assertEqual(heads, {"rust": "finished"})


class HandsWork(unittest.TestCase):
    hands = {"12": {"sha": "a", "files": ["x.rs"], "parent": None},
             "9": {"sha": "b", "files": ["y.rs"], "parent": 7}}

    def test_the_lowest_numbered_rebase_starts_first(self):
        n, task = rebases.next_hands(self.hands, {}, 0, 4, False)
        self.assertEqual(n, 9)
        self.assertIn("onto PR #7 head", task)

    def test_no_rebase_starts_when_every_slot_is_taken(self):
        self.assertIsNone(rebases.next_hands(self.hands, {}, 4, 4, False))

    def test_no_rebase_starts_while_the_machine_is_busy(self):
        self.assertIsNone(rebases.next_hands(self.hands, {}, 0, 4, True))

    def test_one_rebase_starts_at_a_time_as_slots_free(self):
        agents = {"hands-9": {"status": "running", "task": rebases.hands_task(9, self.hands["9"])}}
        self.assertEqual(rebases.next_hands(self.hands, agents, 3, 4, False)[0], 12)
        self.assertIsNone(rebases.next_hands(self.hands, agents, 4, 4, False))

    def test_a_rebase_already_tried_for_the_same_conflict_is_not_started_again(self):
        agents = {f"hands-{n}": {"status": "done", "task": rebases.hands_task(int(n), h)} for n, h in self.hands.items()}
        self.assertIsNone(rebases.next_hands(self.hands, agents, 0, 4, False))

    def test_a_new_conflict_after_a_finished_rebase_starts_another(self):
        agents = {"hands-9": {"status": "done", "task": "an older brief"}}
        self.assertEqual(rebases.next_hands({"9": self.hands["9"]}, agents, 0, 4, False)[0], 9)

    def test_a_rebase_waits_while_its_pr_is_in_an_open_train(self):
        self.assertIsNone(rebases.next_hands(self.hands, {}, 0, 4, False, {9, 12}))

    def test_a_running_hands_task_is_not_resumed_inside_an_open_train(self):
        self.assertTrue(rebases.in_open_train("hands-9", {9}))
        self.assertFalse(rebases.in_open_train("hands-12", {9}))


class Mergeable(unittest.TestCase):
    def test_a_conflicting_pr_conflicts(self):
        self.assertTrue(plan.is_conflicting("CONFLICTING", False))

    def test_a_mergeable_pr_does_not_conflict(self):
        self.assertFalse(plan.is_conflicting("MERGEABLE", True))

    def test_unknown_keeps_a_conflict_that_was_reported(self):
        self.assertTrue(plan.is_conflicting("UNKNOWN", True))

    def test_unknown_keeps_a_pr_that_merged(self):
        self.assertFalse(plan.is_conflicting("UNKNOWN", False))


class AreaWork(unittest.TestCase):
    todo = [{"key": "t1", "task": "one", "area": "transfers"}, {"key": "t2", "task": "two", "area": "transfers"},
            {"key": "k1", "task": "kyc", "area": "kyc"}, {"key": "free", "task": "no area"}]
    finished = {"status": "done", "pr": {"number": 7, "branch": "first-branch", "how": "stated"}}

    def next(self, agents, standing=None):
        return plan.next_work(self.todo, agents, 0, 4, False, standing or {})

    def test_a_second_item_in_an_area_waits_while_the_first_is_running(self):
        agents = {"t1": {"status": "running"}, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertIsNone(self.next(agents))

    def test_a_second_item_waits_while_the_first_prs_has_not_passed(self):
        agents = {"t1": self.finished, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertIsNone(self.next(agents, {7: "waiting"}))

    def test_a_second_item_starts_once_the_first_pr_has_passed_and_builds_on_it(self):
        agents = {"t1": self.finished, "k1": {"status": "running"}, "free": {"status": "running"}}
        item = self.next(agents, {7: "passed"})
        self.assertEqual((item["key"], item["base"]), ("t2", {"on": "pr", "pr": 7, "branch": "first-branch"}))

    def test_a_second_item_starts_on_main_once_the_first_pr_has_merged(self):
        agents = {"t1": self.finished, "k1": {"status": "running"}, "free": {"status": "running"}}
        item = self.next(agents, {7: "merged"})
        self.assertEqual((item["key"], item["base"]), ("t2", {"on": "main"}))

    def test_other_areas_and_items_without_an_area_are_not_held_up(self):
        agents = {"t1": {"status": "running"}}
        self.assertEqual(self.next(agents)["key"], "k1")
        agents["k1"] = {"status": "running"}
        self.assertEqual(self.next(agents)["key"], "free")

    def test_an_item_without_an_area_is_returned_as_queued(self):
        self.assertEqual(plan.next_work([{"key": "free", "task": "x"}], {}, 0, 4, False), {"key": "free", "task": "x"})

    def test_a_finished_item_with_no_pr_does_not_hold_its_area(self):
        agents = {"t1": {"status": "done", "pr": None}, "k1": {"status": "running"}, "free": {"status": "running"}}
        item = self.next(agents)
        self.assertEqual((item["key"], item["base"]), ("t2", {"on": "main"}))
        self.assertIn("no PR to wait for", plan.area_report(self.todo, agents, {}, {})["transfers"][0])

    def test_a_finished_item_whose_pr_is_not_yet_looked_for_still_holds_its_area(self):
        agents = {"t1": {"status": "done"}, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertIsNone(self.next(agents))
        self.assertIn("looking for the PR of t1", plan.area_report(self.todo, agents, {}, {})["transfers"][1])

    def test_a_guessed_pr_blocks_its_area_until_a_person_attaches_it(self):
        guessed = {"status": "done", "pr": {"number": 7, "branch": "first-branch", "how": "guessed"}}
        agents = {"t1": guessed, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertIsNone(self.next(agents, {7: "merged"}))
        self.assertIn("guessed", plan.area_report(self.todo, agents, {7: "merged"}, {})["transfers"][0])
        agents["t1"] = {"status": "done", "pr": {**guessed["pr"], "how": "attached"}}
        self.assertEqual(self.next(agents, {7: "merged"})["key"], "t2")

    def test_a_pr_none_statement_blocks_its_area_and_says_why(self):
        agents = {"t1": {"status": "done", "pr": None, "stated_no_pr": True}, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertIsNone(self.next(agents, {7: "merged"}))
        self.assertIn("stated `PR: none`", plan.area_report(self.todo, agents, {}, {})["transfers"][1])

    def test_a_resumed_run_counts_for_its_task(self):
        agents = {"t1": {"status": "unfinished"}, "t1~2": self.finished, "k1": {"status": "running"}, "free": {"status": "running"}}
        self.assertEqual(self.next(agents, {7: "merged"})["key"], "t2")

    def test_the_brief_says_what_to_build_on(self):
        stacked = plan.stacked_task({"task": "do it", "area": "a", "base": {"on": "pr", "pr": 7, "branch": "b"}})
        self.assertIn("--base 7", stacked)
        self.assertIn("stacks on #7", stacked)
        self.assertIn("upstream/main", plan.stacked_task({"task": "do it", "area": "a", "base": {"on": "main"}}))

    def test_the_report_says_what_each_area_waits_on(self):
        agents = {"t1": self.finished}
        report = plan.area_report(self.todo, agents, {7: "waiting"}, {})
        self.assertEqual(report["transfers"], ["t1: done, PR #7 waiting", "t2: queued, waiting on #7 to pass a batch"])
        self.assertEqual(report["kyc"], ["k1: queued, ready to start"])


class StatedPr(unittest.TestCase):
    def test_the_last_pr_line_wins(self):
        self.assertEqual(plan.stated_pr("PR: #3\nmore work\nPR: #854\n"), 854)

    def test_the_stated_line_wins_over_a_guess_from_the_log(self):
        log = "pushed to alpha, see #2\nPR: #9"
        self.assertEqual(plan.find_pr([], log, FindPr.prs, 50, "me")["number"], 1)
        self.assertEqual(plan.stated_pr(log), 9)

    def test_pr_none_is_read_as_none(self):
        self.assertEqual(plan.stated_pr("done\nPR: none"), "none")

    def test_a_log_without_the_line_states_nothing(self):
        self.assertIsNone(plan.stated_pr("opened PR #12 and wrote PR: #12 inline"))


class FindPr(unittest.TestCase):
    prs = [{"number": 1, "headRefName": "alpha", "owner": "me", "state": "OPEN", "updatedAt": 100},
           {"number": 2, "headRefName": "beta", "owner": "me", "state": "OPEN", "updatedAt": 100},
           {"number": 3, "headRefName": "alpha", "owner": "other", "state": "OPEN", "updatedAt": 100},
           {"number": 4, "headRefName": "gamma", "owner": "me", "state": "MERGED", "updatedAt": 100}]

    def test_the_branch_in_its_worktree_names_its_pr(self):
        self.assertEqual(plan.find_pr(["beta"], "done", self.prs, 50, "me"), {"number": 2, "branch": "beta"})

    def test_the_branch_in_its_log_names_its_pr(self):
        self.assertEqual(plan.find_pr([], "pushed to alpha, PR open", self.prs, 50, "me")["number"], 1)

    def test_a_branch_that_merely_contains_a_name_does_not_match(self):
        self.assertIsNone(plan.find_pr([], "pushed to alphabet", self.prs, 50, "me"))

    def test_a_merged_pr_is_found_when_no_open_one_is(self):
        self.assertEqual(plan.find_pr(["gamma"], "", self.prs, 50, "me")["number"], 4)

    def test_two_branches_are_told_apart_by_the_number_the_log_gives(self):
        self.assertEqual(plan.find_pr(["alpha", "beta"], "PR #2", self.prs, 50, "me")["number"], 2)
        self.assertIsNone(plan.find_pr(["alpha", "beta"], "done", self.prs, 50, "me"))

    def test_a_pr_updated_since_the_start_and_named_in_the_log_is_found(self):
        self.assertEqual(plan.find_pr([], "opened #2", self.prs, 50, "me")["number"], 2)
        self.assertIsNone(plan.find_pr([], "opened #2", self.prs, 500, "me"))

    def test_a_branch_pushed_by_somebody_else_is_ignored(self):
        self.assertEqual(plan.find_pr(["alpha"], "", self.prs, 50, "me")["number"], 1)

    def test_a_pr_standing_is_merged_passed_or_waiting(self):
        self.assertEqual(plan.pr_standing("h", {"sha": "h"}, True), "merged")
        self.assertEqual(plan.pr_standing("h", {"sha": "h"}, False), "passed")
        self.assertEqual(plan.pr_standing("h2", {"sha": "h"}, False), "waiting")
        self.assertEqual(plan.pr_standing("h", None, False), "waiting")


class Logs(unittest.TestCase):
    def test_a_full_disk_is_infrastructure(self):
        self.assertEqual(plan.classify("rustc-LLVM ERROR: No space left on device"), "infrastructure")

    def test_a_missing_tool_is_infrastructure(self):
        self.assertEqual(plan.classify("scripts/checks.sh: line 138: cargo: command not found"), "infrastructure")

    def test_a_failed_test_beside_machine_noise_is_code(self):
        log = ("FATAL:  terminating connection due to administrator command\n"
               "test payout::histories::a_payout_leg_pays_at_most_once ... FAILED\n")
        self.assertEqual(plan.classify(log), "code")

    def test_a_missing_cache_blob_is_infrastructure(self):
        log = "ERROR: failed to solve: failed to compute cache key: not found: content digest sha256:9f2c1e\n"
        self.assertEqual(plan.classify(log), "infrastructure")

    def test_a_failed_layer_export_is_infrastructure(self):
        log = "ERROR: failed to solve: failed to export layer: write /var/lib/docker/overlay2: input/output error\n"
        self.assertEqual(plan.classify(log), "infrastructure")

    def test_a_vanished_image_is_infrastructure(self):
        log = "Error response from daemon: No such image: sha256:4be1a0\n"
        self.assertEqual(plan.classify(log), "infrastructure")

    def test_a_failed_test_beside_a_cache_failure_is_code(self):
        log = ("failed to export layer\n"
               "test payout::histories::a_payout_leg_pays_at_most_once ... FAILED\n")
        self.assertEqual(plan.classify(log), "code")

    def test_a_bad_dockerfile_is_code(self):
        log = "ERROR: failed to solve: process \"/bin/sh -c cargo build\" did not complete successfully: exit code: 101\n"
        self.assertEqual(plan.classify(log), "code")

    def test_a_failed_assertion_is_code(self):
        self.assertEqual(plan.classify("assertion `left == right` failed"), "code")

    def test_failed_tests_are_named(self):
        log = ("test worker::tests::a_slow_pass ... FAILED\n"
               "NumbersFlowTest > a_number_is_registered FAILED\n"
               "  ✘   40 [chromium] › e2e/console.spec.ts:1013:3 › the audit log (7.2s)\n")
        self.assertEqual(plan.failed_tests(log), [
            "NumbersFlowTest > a_number_is_registered",
            "e2e/console.spec.ts:1013:3 › the audit log",
            "worker::tests::a_slow_pass",
        ])


if __name__ == "__main__":
    unittest.main()


class FlakeLedger(unittest.TestCase):
    def test_a_flake_is_counted_with_when_and_where(self):
        ledger = plan.record_flakes({}, ["a::slow"], "runs/b-r0", 100.0)
        self.assertEqual(ledger, {"a::slow": {"count": 1, "last_seen": 100.0, "runs": ["runs/b-r0"]}})

    def test_the_same_run_is_not_counted_twice(self):
        once = plan.record_flakes({}, ["a::slow"], "runs/b-r0", 100.0)
        self.assertEqual(plan.record_flakes(once, ["a::slow"], "runs/b-r0", 200.0), once)

    def test_recording_leaves_the_old_ledger_alone(self):
        before = plan.record_flakes({}, ["a::slow"], "r0", 1.0)
        plan.record_flakes(before, ["a::slow"], "r1", 2.0)
        self.assertEqual(before["a::slow"]["runs"], ["r0"])

    def test_a_test_flaking_twice_gets_a_sonnet_task_naming_it(self):
        ledger = plan.record_flakes(plan.record_flakes({}, ["a::slow"], "r0", 1.0), ["a::slow"], "r1", 2.0)
        [task] = plan.flake_tasks(ledger, set(), lambda test, runs: [(r, f"{test} panicked") for r in runs])
        self.assertEqual((task["key"], task["model"]), ("flake-a-slow", "sonnet"))
        for text in ("a::slow", "a::slow panicked", "never by a longer timeout", "10 times under 4 busy loops", "draft PR"):
            self.assertIn(text, task["task"])

    def test_one_flake_queues_nothing(self):
        ledger = plan.record_flakes({}, ["a::slow"], "r0", 1.0)
        self.assertEqual(plan.flake_tasks(ledger, set(), lambda test, runs: []), [])

    def test_a_test_with_a_task_already_is_not_queued_again(self):
        ledger = plan.record_flakes(plan.record_flakes({}, ["a::slow"], "r0", 1.0), ["a::slow"], "r1", 2.0)
        self.assertEqual(plan.flake_tasks(ledger, {"flake-a-slow"}, lambda test, runs: []), [])

    def test_long_names_that_share_a_start_get_different_keys(self):
        stem = "crate::module::tests::" + "x" * 40
        self.assertNotEqual(plan.flake_key(stem + "one"), plan.flake_key(stem + "two"))

    def test_excerpts_are_the_lines_naming_the_test(self):
        log = "ok\ntest worker::tests::slow ... FAILED\nunrelated\nthread 'worker::tests::slow' panicked"
        self.assertEqual(plan.lines_about(log, "worker::tests::slow"), "test worker::tests::slow ... FAILED\nthread 'worker::tests::slow' panicked")

    def test_flakes_list_by_count_then_most_recent(self):
        ledger = {"a": {"count": 1, "last_seen": 9, "runs": []}, "b": {"count": 3, "last_seen": 1, "runs": []},
                  "c": {"count": 1, "last_seen": 5, "runs": []}}
        self.assertEqual(plan.flake_rows(ledger), [("b", 3, 1), ("a", 1, 9), ("c", 1, 5)])


class ChangesToHowChecksRunAreBatchedAlone(unittest.TestCase):
    def test_of_two_pipeline_changes_only_one_starts_and_it_does_not_take_an_ordinary_change_with_it(self):
        waiting = [pr(1, ci=True), pr(2, ci=True), pr(3)]
        started = plan.batches(waiting, {}, set(), 5)
        self.assertEqual([(g, m) for g, m, _ in started], [("ci", [1]), ("core", [3])])

    def test_a_pipeline_change_does_not_start_while_another_is_running(self):
        started = plan.batches([pr(2, ci=True)], {}, {"ci"}, 5)
        self.assertEqual(started, [])

    def test_the_pipeline_files_are_recognised(self):
        self.assertTrue(plan.touches_ci([".github/workflows/ci.yaml"]))
        self.assertFalse(plan.touches_ci(["scripts/new-migration.sh", "docs/ci-runners.md"]))
