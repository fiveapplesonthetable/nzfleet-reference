import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nzfleet import agents, openpr, plan, procs, prove, state, world


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Waiting(unittest.TestCase):
    def test_a_process_that_ends_returns_zero_and_says_nothing_before_a_beat(self):
        clock, said = Clock(), []
        left = [5]

        def alive():
            left[0] -= 1
            return left[0] > 0
        self.assertEqual(procs.wait_for(alive, said.append, clock, clock.sleep), 0)
        self.assertEqual(said, [])

    def test_a_running_process_is_reported_every_minute_and_returns_three_at_the_limit(self):
        clock, said = Clock(), []
        self.assertEqual(procs.wait_for(lambda: True, said.append, clock, clock.sleep, limit=540, beat=60), 3)
        self.assertEqual(len(said), 8)
        self.assertTrue(all(t % 60 == 0 or t >= 60 for t in said))

    def test_the_default_limit_is_under_the_ten_minutes_a_tool_call_may_last(self):
        self.assertLess(procs.WAIT_LIMIT, 600)


class Processes(unittest.TestCase):
    def setUp(self):
        self.wt = os.path.realpath(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.wt)

    def tearDown(self):
        for name in os.listdir(os.path.join(self.wt, ".nzrun")) if os.path.isdir(os.path.join(self.wt, ".nzrun")) else []:
            if name.endswith(".pid"):
                procs.kill(name[:-4], self.wt)
        os.chdir(self.cwd)

    def say(self):
        out = []
        return out, out.append

    def test_a_command_that_ends_is_waited_for_with_its_exit_code_and_log(self):
        self.assertEqual(procs.start("quick", ["bash", "-c", "echo hello; exit 3"], self.wt), "")
        out, say = self.say()
        self.assertEqual(procs.wait("quick", self.wt, say), 0)
        self.assertIn("exit code 3", out[0])
        self.assertIn("FAILED", out[0])
        self.assertIn("hello", out[1])

    def test_a_command_still_running_at_the_limit_says_call_again(self):
        procs.start("slow", ["sleep", "30"], self.wt)
        out, say = self.say()
        with mock.patch.object(procs, "WAIT_LIMIT", 1):
            self.assertEqual(procs.wait("slow", self.wt, say), 3)
        self.assertIn("nzfleet wait slow", out[-1])

    def test_a_name_started_twice_is_refused_while_it_runs(self):
        procs.start("twice", ["sleep", "30"], self.wt)
        self.assertIn("already running", procs.start("twice", ["sleep", "30"], self.wt))

    def test_kill_stops_the_process_and_what_it_started(self):
        ready = os.path.join(self.wt, "ready")
        os.mkfifo(ready)
        procs.start("tree", ["bash", "-c", f"sleep 60 & echo up > {ready}; wait"], self.wt)
        with open(ready) as started:
            started.read()
        pid = int(open(os.path.join(self.wt, ".nzrun", "tree.pid")).read())
        children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
        self.assertTrue(children)
        self.assertEqual(procs.kill("tree", self.wt), "")
        self.assertFalse(procs.alive(pid))
        self.assertFalse(any(procs.alive(int(c)) for c in children))

    def test_kill_waits_for_a_child_that_outlives_the_leader(self):
        ready = os.path.join(self.wt, "ready")
        os.mkfifo(ready)
        script = (
            "import os, signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGUSR1, lambda *_: exit(0))\n"
            f"with open({ready!r}, 'w') as f: f.write(str(os.getpid()))\n"
            "while True: signal.pause()\n"
        )
        procs.start("tree", [sys.executable, "-c", script], self.wt)
        with open(ready) as started:
            child = int(started.read())
        with open(os.path.join(self.wt, ".nzrun", "tree.pid")) as saved:
            leader = int(saved.read())
        wait_for = procs.wait_for

        def wait_for_group(is_alive, say):
            self.assertEqual(wait_for(lambda: procs.alive(leader), lambda _: None), 0)
            try:
                self.assertTrue(is_alive(), "the child still runs after its leader exits")
            finally:
                os.kill(child, signal.SIGUSR1)
            return wait_for(is_alive, say)

        try:
            with mock.patch.object(procs, "wait_for", side_effect=wait_for_group) as waiting:
                self.assertEqual(procs.kill("tree", self.wt), "")
                waiting.assert_called_once()
            self.assertFalse(procs.alive(child))
        finally:
            if procs.alive(child):
                os.kill(child, signal.SIGKILL)

    def test_kill_refuses_a_process_running_outside_the_worktree(self):
        elsewhere = subprocess.Popen(["sleep", "30"], cwd="/", start_new_session=True)
        try:
            os.makedirs(os.path.join(self.wt, ".nzrun"))
            open(os.path.join(self.wt, ".nzrun", "theirs.pid"), "w").write(str(elsewhere.pid))
            self.assertIn("not touching it", procs.kill("theirs", self.wt))
            self.assertTrue(procs.alive(elsewhere.pid))
        finally:
            elsewhere.kill()
            elsewhere.wait()

    def test_kill_now_stops_a_child_that_ignores_sigterm(self):
        ready = os.path.join(self.wt, "ready")
        os.mkfifo(ready)
        procs.start("tree", ["bash", "-c", f"trap '' TERM; echo $$ > {ready}; exec sleep 60"], self.wt)
        with open(ready) as started:
            child = int(started.read())
        self.assertEqual(procs.kill("tree", self.wt, now=True), "")
        self.assertFalse(procs.alive(child))

    def test_waiting_for_a_name_never_started_says_how_to_start_it(self):
        out, say = self.say()
        self.assertEqual(procs.wait("nope", self.wt, say), 1)
        self.assertIn("nzfleet run nope", out[0])

    def test_a_check_prints_where_it_is_while_it_runs(self):
        out, say = self.say()
        log = os.path.join(self.wt, "log")
        code = prove.run_check(["bash", "-c", "echo started; sleep 3"], self.wt, log, say, beat=1)
        self.assertEqual(code, 0)
        self.assertTrue(out and "last line: started" in out[0])

    def test_a_check_that_outlives_its_limit_is_killed_and_fails(self):
        log = os.path.join(self.wt, "log")
        self.assertEqual(prove.run_check(["sleep", "30"], self.wt, log, lambda _: None, beat=1, limit=1), 124)
        self.assertIn("timed out", open(log).read())

    def test_a_missing_tool_is_said_plainly(self):
        log = os.path.join(self.wt, "log")
        self.assertEqual(prove.run_check(["no-such-tool-nz"], self.wt, log, lambda _: None), 127)
        self.assertIn("no-such-tool-nz is not installed", open(log).read())


class ProcessGroups(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.proc = directory.name

    def stat(self, pid, group, state="S", name="worker"):
        directory = os.path.join(self.proc, str(pid))
        os.mkdir(directory)
        with open(os.path.join(directory, "stat"), "w") as f:
            f.write(f"{pid} ({name}) {state} 1 {group} 0\n")

    def test_a_live_child_counts_after_its_leader_is_a_zombie(self):
        self.stat(42, 42, state="Z")
        self.stat(43, 42)
        self.assertTrue(procs.group_alive(42, self.proc))

    def test_zombies_and_other_groups_do_not_keep_a_group_alive(self):
        self.stat(42, 42, state="Z")
        self.stat(43, 43)
        self.assertFalse(procs.group_alive(42, self.proc))

    def test_a_process_name_can_contain_spaces_and_closing_parentheses(self):
        self.stat(43, 42, name="a worker ) (42)")
        self.assertTrue(procs.group_alive(42, self.proc))

    def test_a_process_that_disappears_during_the_scan_is_ignored(self):
        os.mkdir(os.path.join(self.proc, "42"))
        os.mkdir(os.path.join(self.proc, "self"))
        self.assertFalse(procs.group_alive(42, self.proc))


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Branch(unittest.TestCase):
    """A repository with a base commit and an own commit, as `nzfleet start` leaves one."""

    def setUp(self):
        local_git_vars = subprocess.run(["git", "rev-parse", "--local-env-vars"], check=True,
                                        capture_output=True, text=True).stdout.splitlines()
        clean_env = {key: value for key, value in os.environ.items()
                     if key not in local_git_vars and key != "GIT_NAMESPACE"}
        git_environment = mock.patch.dict(os.environ, clean_env, clear=True)
        git_environment.start()
        self.addCleanup(git_environment.stop)
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.wt = os.path.join(self.dir, "wt")
        self.origin = os.path.join(self.dir, "origin.git")
        git(self.dir, "init", "-q", "--bare", self.origin)
        os.mkdir(self.wt)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "remote", "add", "origin", self.origin)
        open(os.path.join(self.wt, "a.txt"), "w").write("one\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "Base")
        self.base = git(self.wt, "rev-parse", "HEAD")
        git(self.wt, "switch", "-q", "-c", "a-branch")
        with open(os.path.join(self.wt, ".git", "info", "exclude"), "a") as f:
            f.write(".nzenv\n.nzproof\n.nzrun\n")
        open(os.path.join(self.wt, ".nzenv"), "w").write(plan.env_file({"NZ_BASE_SHA": self.base, "NZ_SLUG": "a-branch"}))
        self.cwd = os.getcwd()
        os.chdir(self.wt)

    def tearDown(self):
        os.chdir(self.cwd)

    def commit(self, message="A line is now two\n\nBecause."):
        open(os.path.join(self.wt, "a.txt"), "a").write("two\n")
        git(self.wt, "commit", "-qam", message)

    def prove(self, ok=True):
        os.makedirs(os.path.join(self.wt, ".nzproof"), exist_ok=True)
        proof = {"head": git(self.wt, "rev-parse", "HEAD"), "tree": prove.tree_of_files(self.wt), "ok": ok, "checks": []}
        json.dump(proof, open(os.path.join(self.wt, ".nzproof", "summary.json"), "w"))

    def pr(self, dry_run=True, message="Because.", shots_anyway=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(world, "gh_json", return_value=[]):
            code = openpr.pr("A line is now two", message, dry_run, shots_anyway)
        return code, out.getvalue()

    def commit_shot(self, name):
        path = os.path.join(self.wt, "frontend", "admin", "screenshots", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("x\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "A shot\n\nBecause.")

    def test_a_screenshot_no_changed_screen_draws_is_refused_and_named(self):
        self.commit()
        self.commit_shot("05-transfer-detail-waiting.png")
        self.prove()
        code, out = self.pr()
        self.assertEqual(code, 2)
        self.assertIn("frontend/admin/screenshots/05-transfer-detail-waiting.png", out)

    def test_shots_anyway_lets_the_screenshot_through(self):
        self.commit()
        self.commit_shot("05-transfer-detail-waiting.png")
        self.prove()
        self.assertEqual(self.pr(shots_anyway=True)[0], 0)

    def test_the_tree_of_the_files_on_disk_is_the_tree_of_a_commit_of_them(self):
        open(os.path.join(self.wt, "a.txt"), "a").write("two\n")
        open(os.path.join(self.wt, "new.txt"), "w").write("new\n")
        open(os.path.join(self.wt, ".nzenv"), "a").write("# ignored\n")
        before = prove.tree_of_files(self.wt)
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "Both")
        self.assertEqual(git(self.wt, "rev-parse", "HEAD^{tree}"), before)

    def test_changed_files_include_commits_edits_and_new_files(self):
        self.commit()
        open(os.path.join(self.wt, "b.txt"), "w").write("x\n")
        self.assertEqual(prove.changed_files(self.wt, self.base), ["a.txt", "b.txt"])

    def test_changed_files_include_both_sides_of_a_rename(self):
        self.commit()
        os.rename(os.path.join(self.wt, "a.txt"), os.path.join(self.wt, "moved.txt"))
        self.assertEqual(prove.changed_files(self.wt, self.base), ["a.txt", "moved.txt"])

    def test_a_branch_with_nothing_changed_is_refused(self):
        self.prove()
        code, out = self.pr()
        self.assertEqual(code, 2)
        self.assertIn("changes nothing", out)

    def test_a_proven_commit_prints_the_pr_it_would_open(self):
        self.commit()
        self.prove()
        code, out = self.pr()
        self.assertEqual(code, 0, out)
        self.assertIn("would run: git push --no-verify origin HEAD:refs/heads/a-branch", out)
        self.assertIn("draft PR", out)

    def test_a_commit_made_after_the_proof_is_refused_as_older(self):
        self.commit()
        self.prove()
        open(os.path.join(self.wt, "a.txt"), "a").write("three\n")
        git(self.wt, "commit", "-qam", "Three")
        code, out = self.pr()
        self.assertEqual(code, 2)
        self.assertIn("older than the last commit", out)

    def test_a_failed_proof_is_refused(self):
        self.commit()
        self.prove(ok=False)
        self.assertEqual(self.pr()[0], 2)

    def test_a_missing_proof_is_refused(self):
        self.commit()
        self.assertIn("there is no proof", self.pr()[1])

    def test_an_attributed_commit_is_refused_even_when_proven(self):
        self.commit("A line is now two\n\nCo-Authored-By: A Tool <t@example.com>")
        self.prove()
        code, out = self.pr()
        self.assertEqual(code, 2)
        self.assertIn("Co-Authored-By", out)

    def test_a_merge_commit_is_refused(self):
        self.commit()
        git(self.wt, "switch", "-q", "-c", "side", self.base)
        open(os.path.join(self.wt, "s.txt"), "w").write("s\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "Side")
        git(self.wt, "switch", "-q", "a-branch")
        git(self.wt, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.prove()
        code, out = self.pr()
        self.assertEqual(code, 2)
        self.assertIn("merge commits", out)

    def test_an_update_pushes_with_a_lease(self):
        self.commit()
        git(self.wt, "push", "-q", "origin", "a-branch")
        self.prove()
        self.assertIn("--force-with-lease=refs/heads/a-branch:", self.pr()[1])


class Reaping(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        state.PATH = os.path.join(self.home, "state.json")
        state.LOCK = os.path.join(self.home, "state.lock")

    def agent(self, log_text):
        log = os.path.join(self.home, "a.log")
        open(log, "w").write(log_text)
        with state.change() as s:
            s["agents"]["a"] = {"status": "running", "pid": 2 ** 22 + 7, "task": "do it", "model": "sonnet", "log": log, "started": 0}

    def test_a_run_that_ended_with_a_summary_is_done(self):
        self.agent("PR #9 is up.\n")
        self.assertEqual(agents.reap(), ["a"])
        self.assertEqual(state.read()["agents"]["a"]["status"], "done")

    def test_a_run_that_ended_waiting_on_the_background_is_unfinished(self):
        self.agent("It is running in the background; I'll check when it lands.\n")
        agents.reap()
        self.assertEqual(state.read()["agents"]["a"]["status"], "unfinished")

    def recorded(self, log_text):
        self.agent(log_text)
        agents.reap()
        guess = {"number": 2, "branch": "alpha", "owner": "me", "state": "OPEN", "headRefName": "alpha", "updatedAt": 100}
        with mock.patch.object(world, "fork_prs", return_value=[guess]), \
                mock.patch.object(world, "worktree_branches_between", return_value=["alpha"]), \
                mock.patch.object(world, "branch_of", return_value="stated-branch"), \
                mock.patch.object(agents, "FORK", "me"):
            agents.record_prs()
        return state.read()["agents"]["a"]

    def test_the_pr_an_agent_states_is_recorded_as_stated_over_the_one_its_branch_suggests(self):
        run = self.recorded("pushed alpha\nPR: #854\n")
        self.assertEqual(run["pr"], {"number": 854, "branch": "stated-branch", "how": "stated"})

    def test_a_pr_found_without_a_statement_is_recorded_as_a_guess(self):
        run = self.recorded("pushed alpha, PR #2 is up\n")
        self.assertEqual((run["pr"]["number"], run["pr"]["how"]), (2, "guessed"))

    def test_pr_none_records_no_pr_and_says_it_was_stated(self):
        run = self.recorded("nothing to change\nPR: none\n")
        self.assertEqual((run["pr"], run["stated_no_pr"]), (None, True))


if __name__ == "__main__":
    unittest.main()
