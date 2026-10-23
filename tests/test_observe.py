import os
import tempfile
import unittest

from nzfleet import observe, supervise

from fleet import sh

NOW = 1_800_000_000.0


class ProcessTable(unittest.TestCase):
    def setUp(self):
        self.proc = os.path.realpath(tempfile.mkdtemp())
        open(os.path.join(self.proc, "uptime"), "w").write("10000.00 5000.00\n")

    def process(self, pid, name, ppid, utime, stime, cutime, cstime, start, cwd="/tmp", env=b""):
        base = os.path.join(self.proc, str(pid))
        os.makedirs(base)
        fields = ["S", str(ppid), str(pid), "0", "0", "0", "0", "0", "0", "0", "0", str(utime), str(stime), str(cutime), str(cstime),
                  "20", "0", "1", "0", str(start)]
        open(os.path.join(base, "stat"), "w").write(f"{pid} ({name}) " + " ".join(fields) + "\n")
        os.symlink(cwd, os.path.join(base, "cwd"))
        open(os.path.join(base, "environ"), "wb").write(env)

    def table(self):
        return {p.pid: p for p in observe.processes(self.proc, ticks=100)}

    def test_cpu_is_user_system_and_waited_for_children_in_seconds(self):
        self.process(5, "claude", 1, 1000, 500, 300, 200, 0)
        self.assertEqual(self.table()[5].cpu, 20.0)

    def test_age_is_uptime_minus_start_time(self):
        self.process(5, "yes", 1, 0, 0, 0, 0, 940000)
        self.assertEqual(self.table()[5].age, 600.0)

    def test_a_name_with_spaces_and_brackets_does_not_shift_the_fields(self):
        self.process(5, "tmux: server (x)", 7, 100, 0, 0, 0, 0)
        self.assertEqual((self.table()[5].name, self.table()[5].ppid), ("tmux: server (x)", 7))

    def test_the_starvation_marker_is_read_from_the_environment(self):
        self.process(5, "yes", 1, 0, 0, 0, 0, 0, env=b"A=b\0NZFLEET_STARVE=1\0")
        self.process(6, "yes", 1, 0, 0, 0, 0, 0, env=b"A=b\0")
        table = self.table()
        self.assertEqual((table[5].starved, table[6].starved), (True, False))

    def test_a_burner_in_a_run_scope_is_found_by_its_cgroup_and_its_timeout_wrapper_by_its_command_line(self):
        scope = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-u16085.scope"
        for pid, name, argv in ((5, "yes", "yes"), (6, "timeout", "timeout\x007200\x00yes"), (7, "cargo", "cargo\x00test")):
            self.process(pid, name, 1, 0, 0, 0, 0, 0)
            base = os.path.join(self.proc, str(pid))
            open(os.path.join(base, "cmdline"), "w").write(argv + "\0")
            open(os.path.join(base, "cgroup"), "w").write(f"0::{scope}\n")
        found = [p.pid for p in supervise.burners(list(self.table().values()), set(), "/w/work")]
        self.assertEqual(sorted(found), [5, 6])

    def test_a_process_that_vanished_while_it_was_read_is_skipped(self):
        os.makedirs(os.path.join(self.proc, "9"))
        self.assertEqual(self.table(), {})


class WorktreeActivity(unittest.TestCase):
    def setUp(self):
        self.wt = os.path.realpath(tempfile.mkdtemp())
        sh(self.wt, "git", "init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            sh(self.wt, "git", "config", key, value)
        open(os.path.join(self.wt, "a.txt"), "w").write("a\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-qm", "base")
        self.base = sh(self.wt, "git", "rev-parse", "HEAD")

    def touch(self, name, at):
        path = os.path.join(self.wt, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").write("x\n")
        os.utime(path, (at, at))

    def test_a_worktree_with_nothing_done_shows_nothing(self):
        act = observe.activity(self.wt, self.base)
        self.assertEqual((act.commits, act.files, act.last_commit, act.last_change, act.last_test), (0, 0, None, None, None))

    def test_commits_changed_files_and_the_time_of_the_latest_edit(self):
        open(os.path.join(self.wt, "b.txt"), "w").write("b\n")
        sh(self.wt, "git", "add", "-A")
        sh(self.wt, "git", "commit", "-qm", "b")
        self.touch("b.txt", NOW - 600)
        self.touch("c.txt", NOW - 100)
        act = observe.activity(self.wt, self.base)
        self.assertEqual((act.commits, act.files, act.last_change), (1, 2, NOW - 100))
        self.assertGreater(act.last_commit, 0)

    def test_files_git_ignores_are_not_work(self):
        open(os.path.join(self.wt, ".git", "info", "exclude"), "a").write(".nzrun\n")
        self.touch(".nzrun/x.log", NOW)
        self.assertEqual(observe.activity(self.wt, self.base).files, 0)

    def test_the_last_test_is_when_prove_last_wrote_its_summary(self):
        self.touch(".nzproof/summary.json", NOW - 300)
        self.assertEqual(observe.activity(self.wt, self.base).last_test, NOW - 300)

    def test_a_run_log_with_a_took_line_is_a_test_run_and_one_without_is_not(self):
        self.touch(".nzrun/rust.log", NOW - 200)
        open(os.path.join(self.wt, ".nzrun", "rust.log"), "a").write("rust took 90s\n")
        os.utime(os.path.join(self.wt, ".nzrun", "rust.log"), (NOW - 200, NOW - 200))
        self.touch(".nzrun/build.log", NOW - 10)
        self.assertEqual(observe.activity(self.wt, self.base).last_test, NOW - 200)

    def test_a_worktree_that_is_gone_shows_nothing(self):
        self.assertEqual(observe.activity("/nonexistent/x", self.base).commits, 0)


if __name__ == "__main__":
    unittest.main()
