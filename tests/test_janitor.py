import fcntl
import datetime
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from nzfleet import janitor, tidy

NOW = 1_800_000_000.0
HOUR = 3600


class FakeDocker:
    """Answers the docker and git commands the pass issues from tables, and records the rest."""

    def __init__(self, containers=None):
        self.containers = containers or {}  # name -> (hours up, clients, published port)
        self.calls = []
        self.states = {}  # container -> process states `docker top` lists
        self.remote_branches = ""
        self.status = ""
        self.untracked = ""
        self.binary_diff = None
        self.bundle_fails = False
        self.unique_commits = "1"
        self.networks = {}  # name -> names of the containers attached
        self.removes = {}  # what `docker run ... rm -rf <arg>` deletes: arg -> real path

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["docker", "run"] and argv[-1] in self.removes:
            shutil.rmtree(self.removes[argv[-1]])
            return 0, ""
        if argv[:3] == ["docker", "network", "ls"]:
            return 0, "".join(f"{n}\n" for n in ["bridge", *self.networks])
        if argv[:3] == ["docker", "network", "inspect"]:
            return 0, " ".join(self.networks[argv[-1]]) + "\n"
        if argv[:2] == ["docker", "ps"]:
            return 0, "".join(f"{n}\t127.0.0.1:{p}->5432/tcp\n" for n, (_, _, p) in self.containers.items())
        if argv[:2] == ["docker", "inspect"]:
            return 0, tidy_time(NOW - self.containers[argv[-1]][0] * HOUR)
        if argv[:2] == ["docker", "top"]:
            return 0, "STAT\n" + "".join(f"{s}\n" for s in self.states.get(argv[2], ["Ss"]))
        if argv[:2] == ["docker", "exec"]:
            return 0, f"{self.containers[argv[2]][1]}\n"
        if argv[:2] == ["git", "-C"] and "status" in argv:
            return 0, self.status
        if argv[:2] == ["git", "-C"] and "branch" in argv:
            return 0, self.remote_branches
        if argv[:2] == ["git", "-C"] and "merge-base" in argv:
            return 1, ""
        if argv[:2] == ["git", "-C"] and "symbolic-ref" in argv:
            return 0, "refs/heads/main\n"
        if argv[:2] == ["git", "-C"] and "for-each-ref" in argv:
            return 0, "refs/remotes/upstream/main\n"
        if argv[:2] == ["git", "-C"] and "rev-list" in argv:
            return 0, f"{self.unique_commits}\n"
        if argv[:2] == ["git", "-C"] and "bundle" in argv:
            if self.bundle_fails:
                return 1, ""
            if os.path.isdir(os.path.join(argv[2], ".git")):
                done = subprocess.run(argv, capture_output=True)
                return done.returncode, done.stdout.decode()
            with open(argv[argv.index("create") + 1], "wb"):
                pass
            return 0, ""
        if argv[:2] == ["git", "-C"] and "diff" in argv:
            return 0, "tracked change"
        if argv[:2] == ["git", "-C"] and "ls-files" in argv:
            return 0, self.untracked
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return 0, "abc123\n"
        return 0, ""

    def run_bytes(self, argv):
        self.calls.append(argv)
        if self.binary_diff is not None and "diff" in argv:
            return 0, self.binary_diff
        return 0, b"tracked change"

    def issued(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def tidy_time(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class Machine(unittest.TestCase):
    """A directory tree standing for the machine: build roots, worktrees, /tmp and /proc."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.docker = FakeDocker()
        self.host = janitor.Host(builds=[self.path("nzt")], work=self.path("work"), repo=self.path("repo"),
                                 unpacks=self.path("tmp"), proc=self.path("proc"), run=self.docker,
                                 run_bytes=self.docker.run_bytes, now=lambda: NOW)
        for d in ("nzt", "work", "tmp", "proc"):
            os.makedirs(self.path(d))

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def make(self, *parts, hours_old=0.0):
        full = self.path(*parts)
        os.makedirs(full, exist_ok=True)
        stamp = NOW - hours_old * HOUR
        os.utime(full, (stamp, stamp))
        return full

    def process(self, pid, cwd="/", **env):
        base = self.make("proc", str(pid))
        os.symlink(cwd, os.path.join(base, "cwd"))
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))

    def process_holds(self, pid, path, argv=()):
        base = self.make("proc", str(pid))
        os.makedirs(os.path.join(base, "fd"))
        os.symlink(path, os.path.join(base, "fd", "3"))
        with open(os.path.join(base, "cmdline"), "wb") as f:
            f.write(b"\0".join(os.fsencode(arg) for arg in argv))


class BuildOutput(Machine):
    def test_an_idle_build_dir_is_removed_and_a_fresh_one_kept(self):
        old, fresh = self.make("nzt", "old", hours_old=3), self.make("nzt", "fresh", hours_old=0.1)
        janitor.run(self.host)
        self.assertEqual((os.path.exists(old), os.path.exists(fresh)), (False, True))

    def test_a_quiet_dir_with_a_fresh_file_deep_inside_is_kept(self):
        self.make("nzt", "busy", hours_old=3)
        inner = self.make("nzt", "busy", "debug", "deps", hours_old=0.1)
        janitor.run(self.host)
        self.assertTrue(os.path.exists(inner))

    def test_a_dir_a_process_names_as_its_target_is_kept(self):
        quiet = self.make("nzt", "quiet", hours_old=5)
        self.process(7, CARGO_TARGET_DIR=quiet)
        janitor.run(self.host)
        self.assertTrue(os.path.exists(quiet))

    def test_a_live_process_with_an_open_build_file_keeps_it_while_an_idle_build_is_removed(self):
        held_root = self.make("nzt", "held", hours_old=5)
        held = os.path.join(self.make("nzt", "held", "debug", "deps", hours_old=5), "crate.rlib")
        idle = self.make("nzt", "idle", hours_old=5)
        with open(held, "wb"):
            os.utime(held, (NOW - 5 * HOUR,) * 2)
        self.process_holds(7, held)
        janitor.run(self.host)
        self.assertEqual((os.path.exists(held_root), os.path.exists(idle)), (True, False))

    def test_a_live_process_argument_naming_a_build_dir_keeps_it(self):
        quiet = self.make("nzt", "quiet", hours_old=5)
        self.process_holds(9, "/unused", ("rustc", f"--out-dir={quiet}/debug"))
        janitor.run(self.host)
        self.assertTrue(os.path.exists(quiet))

    def refuse(self, path):
        raise PermissionError(path)

    def test_what_a_container_left_as_root_is_removed_through_docker(self):
        stuck = self.make("nzt", "rootowned", hours_old=5)
        self.host.remove_tree = self.refuse
        self.docker.removes = {"/b/rootowned": stuck}
        lines = janitor.run(self.host)
        self.assertEqual(self.docker.issued("docker", "run"),
                         [["docker", "run", "--rm", "--init", "-v", f"{self.path('nzt')}:/b", "alpine", "rm", "-rf", "/b/rootowned"]])
        self.assertEqual((os.path.exists(stuck), lines), (False, [f"tidied {stuck}: build output untouched for 5 h"]))

    def test_a_dir_nothing_can_remove_is_a_log_line_and_the_rest_still_goes(self):
        stuck = self.make("nzt", "stuck", hours_old=5)
        other = self.make("nzt", "other", hours_old=5)
        self.host.remove_tree = lambda path: self.refuse(path) if path == stuck else shutil.rmtree(path)
        lines = janitor.run(self.host)
        self.assertEqual((os.path.exists(stuck), os.path.exists(other)), (True, False))
        self.assertIn(f"could not tidy {stuck}: {stuck} is still there after trying to remove it", lines)


class Temporaries(Machine):
    def setUp(self):
        super().setUp()
        self.host.tmp = self.path("scratch")
        self.host.state = {"agents": {"done": {"status": "finished"}, "live": {"status": "running"}}}
        os.makedirs(self.host.tmp)

    def test_what_a_finished_agent_left_goes_and_a_persons_file_in_the_root_stays(self):
        old = self.make("scratch", "done", "old", hours_old=6)
        self.make("scratch", "done", hours_old=6)
        mine = self.make("scratch", "notes", hours_old=99)
        janitor.run(self.host)
        self.assertEqual((os.path.exists(old), os.path.exists(mine)), (False, True))

    def test_a_running_agents_directory_is_never_emptied(self):
        held = self.make("scratch", "live", "old", hours_old=99)
        janitor.run(self.host)
        self.assertTrue(os.path.exists(held))

    def test_a_runner_directory_is_emptied_when_idle_and_kept_when_a_job_has_a_file_open(self):
        idle = self.make("scratch", "runner-1", "job", hours_old=6)
        busy = self.make("scratch", "runner-2", "job", hours_old=6)
        self.process_holds(41, os.path.join(busy, "lib.so"))
        janitor.run(self.host)
        self.assertEqual((os.path.exists(idle), os.path.exists(busy)), (False, True))

    def test_a_job_whose_tmpdir_is_the_runners_keeps_it(self):
        busy = self.make("scratch", "runner-3", "job", hours_old=6)
        self.process(42, TMPDIR=self.path("scratch", "runner-3"))
        janitor.run(self.host)
        self.assertTrue(os.path.exists(busy))

    def test_a_library_a_jvm_unpacked_and_mapped_keeps_its_directory(self):
        busy = self.make("scratch", "runner-4", "job", hours_old=6)
        maps = os.path.join(self.make("proc", "43"), "maps")
        with open(maps, "w") as f:
            f.write(f"7f00-7f01 r-xp 0 00:00 1 {busy}/libnative.so\n")
        janitor.run(self.host)
        self.assertTrue(os.path.exists(busy))

    def test_our_stale_leftovers_in_the_system_directory_go_by_prefix_and_others_stay(self):
        ours = self.make("tmp", "nzela-e2e.abc", hours_old=6)
        other = self.make("tmp", "somebody-else", hours_old=99)
        janitor.run(self.host)
        self.assertEqual((os.path.exists(ours), os.path.exists(other)), (False, True))


class Containers(Machine):
    def test_an_old_idle_database_is_removed_and_a_connected_one_kept(self):
        self.docker.containers = {"agentdb-old": (3, 0, 5000), "agentdb-used": (3, 2, 5001), "unrelated": (9, 0, 6000)}
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("docker", "rm"), [["docker", "rm", "-f", "agentdb-old"]])

    def test_a_database_named_in_a_live_processs_environment_is_kept(self):
        self.docker.containers = {"agentdb-mine": (3, 0, 5000)}
        self.process(9, DATABASE_URL="postgres://postgres:nzela@127.0.0.1:5000/nzela")
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("docker", "rm"), [])

    def batch_database(self, record):
        self.docker.containers = {"nzf-manual-5-r0-again-pg": (1, 0, 5000)}
        self.host.state = {"batches": {"manual-5": record}}
        janitor.run(self.host)
        return self.docker.issued("docker", "rm")

    def test_the_database_of_a_batch_whose_process_is_alive_is_kept(self):
        self.assertEqual(self.batch_database({"status": "running", "pid": os.getpid()}), [])

    def test_the_database_of_a_batch_still_starting_is_kept(self):
        self.assertEqual(self.batch_database({"status": "running", "pid": 0}), [])

    def test_the_database_of_a_batch_whose_process_is_gone_is_removed(self):
        self.assertEqual(self.batch_database({"status": "running", "pid": 2**22 + 1}),
                         [["docker", "rm", "-f", "nzf-manual-5-r0-again-pg"]])


class Worktrees(Machine):
    def finished(self, name, hours_old=8):
        path = self.make("work", name, hours_old=hours_old)
        open(os.path.join(path, ".git"), "w").close()
        os.utime(path, (NOW - hours_old * HOUR,) * 2)
        os.utime(os.path.join(path, ".git"), (NOW - hours_old * HOUR,) * 2)
        return path

    def repository(self, name, branch="main"):
        path = self.make("work", name)
        subprocess.run(["git", "-C", path, "init", "-q", "-b", branch], check=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
        return path

    def commit(self, path, name, contents):
        with open(os.path.join(path, name), "wb") as output:
            output.write(contents)
        subprocess.run(["git", "-C", path, "add", name], check=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", name], check=True)

    def test_a_pushed_clean_idle_worktree_is_removed_through_git_and_pruned(self):
        path = self.finished("done")
        self.docker.remote_branches = "  origin/done\n"
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("git", "-C", self.host.repo, "worktree"),
                         [["git", "-C", self.host.repo, "worktree", "remove", "--force", path],
                          ["git", "-C", self.host.repo, "worktree", "prune"]])

    def test_unpublished_commits_are_bundled_before_an_idle_worktree_is_removed(self):
        path = self.finished("mine")
        janitor.run(self.host)
        bundle = self.docker.issued("git", "-C", path, "bundle")
        remove = self.docker.issued("git", "-C", self.host.repo, "worktree")
        self.assertEqual(len(bundle), 1)
        self.assertEqual(remove[0][-1], path)
        self.assertLess(self.docker.calls.index(bundle[0]), self.docker.calls.index(remove[0]))
        rescue = os.path.join(self.path("work", ".rescued"),
                              f"mine-{hashlib.sha256(path.encode()).hexdigest()[:12]}-abc123")
        self.assertTrue(os.path.isfile(os.path.join(rescue, "working-tree.patch")))

    def test_a_head_already_on_main_needs_no_history_bundle(self):
        path = self.repository("main-head")
        self.commit(path, "source", b"already on main")
        subprocess.run(["git", "-C", path, "update-ref", "refs/remotes/upstream/main", "HEAD"], check=True)
        host = janitor.Host(builds=[], work=self.path("work"), repo=self.path("repo"),
                            run=janitor.run_tool, run_bytes=janitor.run_tool_bytes, now=lambda: NOW)

        problem, rescue = janitor._rescue_checkout(host, path)

        self.assertEqual(problem, "")
        self.assertFalse(os.path.exists(os.path.join(rescue, "history.bundle")))
        self.assertTrue(os.path.isfile(os.path.join(rescue, "working-tree.patch")))

    def test_an_unpushed_commit_gets_a_bundle_without_main_history(self):
        path = self.repository("unpushed")
        self.commit(path, "base", b"x" * (1024 * 1024))
        subprocess.run(["git", "-C", path, "update-ref", "refs/remotes/upstream/main", "HEAD"], check=True)
        subprocess.run(["git", "-C", path, "switch", "-q", "-c", "feature"], check=True)
        self.commit(path, "change", b"small change")
        host = janitor.Host(builds=[], work=self.path("work"), repo=self.path("repo"),
                            run=janitor.run_tool, run_bytes=janitor.run_tool_bytes, now=lambda: NOW)

        problem, rescue = janitor._rescue_checkout(host, path)

        bundle_path = os.path.join(rescue, "history.bundle")
        self.assertEqual(problem, "")
        self.assertTrue(os.path.isfile(bundle_path))
        self.assertLess(os.path.getsize(bundle_path), 1024 * 1024)
        heads = subprocess.run(["git", "bundle", "list-heads", bundle_path], check=True,
                               capture_output=True, text=True).stdout
        self.assertIn("refs/heads/feature", heads)

    def test_dirty_tracked_and_untracked_files_are_saved_before_removal(self):
        path = self.finished("dirty")
        self.docker.status = " M source.py\n?? note.txt\n"
        self.docker.untracked = "note.txt\0"
        with open(os.path.join(path, "note.txt"), "w") as note:
            note.write("unfinished")
        lines = janitor.run(self.host)
        rescue = os.path.join(self.path("work", ".rescued"),
                              f"dirty-{hashlib.sha256(path.encode()).hexdigest()[:12]}-abc123")
        self.assertEqual(self.docker.issued("git", "-C", path, "diff")[0][-2:], ["--binary", "HEAD"])
        self.assertTrue(os.path.isfile(os.path.join(rescue, "working-tree.patch")))
        self.assertTrue(os.path.isfile(os.path.join(rescue, "untracked.tar")))
        with tarfile.open(os.path.join(rescue, "untracked.tar")) as archive:
            self.assertEqual(archive.getnames(), ["note.txt"])
        self.assertIn(f"rescue saved to {rescue}", lines[0])
        self.assertEqual(self.docker.issued("git", "-C", self.host.repo, "worktree")[0][-3:-1], ["remove", "--force"])

    def test_a_changed_png_is_saved_as_a_binary_patch(self):
        path = self.finished("png")
        os.remove(os.path.join(path, ".git"))
        subprocess.run(["git", "-C", path, "init", "-q", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True, capture_output=True)
        image = os.path.join(path, "screenshot.png")
        with open(image, "wb") as output:
            output.write(b"\x89PNG\r\n\x1a\n\x00original image")
        subprocess.run(["git", "-C", path, "add", "screenshot.png"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", "base"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "update-ref", "refs/remotes/upstream/main", "HEAD"], check=True)
        with open(image, "wb") as output:
            output.write(b"\x89PNG\r\n\x1a\n\x00changed image with \xa1 byte")
        self.docker.status = " M screenshot.png\n"
        self.docker.unique_commits = "0"
        self.docker.binary_diff = subprocess.run(
            ["git", "-C", path, "diff", "--binary", "HEAD"], capture_output=True, check=True
        ).stdout

        janitor.run(self.host)

        rescue = os.path.join(self.path("work", ".rescued"),
                              f"png-{hashlib.sha256(path.encode()).hexdigest()[:12]}-abc123")
        with open(os.path.join(rescue, "working-tree.patch"), "rb") as saved:
            patch = saved.read()
        self.assertEqual(patch, self.docker.binary_diff)
        self.assertIn(b"GIT binary patch", patch)
        self.assertFalse(os.path.exists(os.path.join(rescue, "history.bundle")))

    def test_a_failed_removal_reuses_the_completed_rescue_for_the_same_head(self):
        repo = self.repository("repo")
        self.commit(repo, "source", b"base")
        path = os.path.join(self.path("work"), "failed-remove")
        subprocess.run(["git", "-C", repo, "worktree", "add", "-qb", "feature", path], check=True)
        self.commit(path, "feature", b"unpublished")
        for base, dirs, files in os.walk(repo):
            os.utime(base, (NOW, NOW))
            for name in files:
                os.utime(os.path.join(base, name), (NOW, NOW))
        old = NOW - 8 * HOUR
        for base, dirs, files in os.walk(path):
            os.utime(base, (old, old))
            for name in files:
                os.utime(os.path.join(base, name), (old, old))
        remove_attempts = []
        fail_removal = [True]

        def run(argv):
            if argv[0] == "docker":
                return self.docker(argv)
            if argv[:5] == ["git", "-C", repo, "worktree", "remove"] and fail_removal[0]:
                remove_attempts.append(argv)
                return 1, ""
            done = subprocess.run(argv, capture_output=True, text=True)
            return done.returncode, done.stdout

        self.host.repo = repo
        self.host.run = run
        self.host.run_bytes = lambda argv: (lambda done: (done.returncode, done.stdout))(
            subprocess.run(argv, capture_output=True))
        janitor.run(self.host)
        janitor.run(self.host)

        rescues = os.listdir(os.path.join(self.path("work"), ".rescued"))
        self.assertEqual(len(remove_attempts), 2, remove_attempts)
        self.assertEqual(len(rescues), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.path("work"), ".rescued", rescues[0], "complete")))
        fail_removal[0] = False
        janitor.run(self.host)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(os.listdir(os.path.join(self.path("work"), ".rescued"))), 1)

    def test_a_throwaway_batch_worktree_is_removed_without_a_rescue(self):
        path = self.finished("nzf-manual-throwaway")

        janitor.run(self.host)

        self.assertFalse(os.path.exists(os.path.join(self.path("work"), ".rescued")))
        self.assertEqual(self.docker.issued("git", "-C", self.host.repo, "worktree", "remove")[0][-1], path)

    def test_over_cap_rescues_prune_oldest_referenced_commits_first(self):
        root = os.path.join(self.path("work"), ".rescued")
        os.makedirs(root)
        old, new, loose = (os.path.join(root, name) for name in ("old", "new", "loose"))
        for path, stamp, head in ((old, 1, "old-head"), (new, 2, "new-head"), (loose, 3, "loose-head")):
            os.makedirs(path)
            with open(os.path.join(path, "head"), "w") as saved:
                saved.write(head)
            with open(os.path.join(path, "payload"), "wb") as saved:
                saved.write(b"large")
            os.utime(path, (stamp, stamp))
        host_run = self.host.run
        self.host.run = lambda argv: (1, "") if "loose-head" in argv else (0, "refs/heads/main\n")

        self.assertEqual(janitor._prune_rescues(self.host, 30), "")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(loose))
        self.host.run = host_run

    def test_a_failed_bundle_keeps_the_worktree(self):
        path = self.finished("dirty")
        self.docker.status = " M source.py\n"
        self.docker.bundle_fails = True
        lines = janitor.run(self.host)
        self.assertTrue(os.path.isdir(path))
        self.assertFalse(self.docker.issued("git", "-C", self.host.repo, "worktree", "remove"))
        self.assertIn("worktree kept", lines[0])

    def test_a_worktree_with_a_process_in_it_is_kept(self):
        path = self.finished("busy")
        self.docker.remote_branches = "  origin/busy\n"
        self.process(3, cwd=path)
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("git", "-C", self.host.repo, "worktree"), [])

    def test_a_running_batchs_worktree_is_kept(self):
        self.finished("nzf-manual-5-r1")
        self.docker.remote_branches = "  origin/x\n"
        self.host.state = {"batches": {"manual-5": {"status": "running", "pid": os.getpid()}}}
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("git", "-C", self.host.repo, "worktree"), [])

    def test_a_recently_touched_worktree_costs_no_git_call(self):
        self.finished("fresh", hours_old=1)
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("git"), [])


class TestRuntimes(Machine):
    def test_only_a_day_old_robolectric_dir_is_removed(self):
        old, young = self.make("tmp", "robolectric-a", hours_old=30), self.make("tmp", "robolectric-b", hours_old=2)
        other = self.make("tmp", "keep-me", hours_old=300)
        janitor.run(self.host)
        self.assertEqual([os.path.exists(p) for p in (old, young, other)], [False, True, True])


class DryRun(Machine):
    def test_a_dry_run_names_what_it_would_do_and_does_nothing(self):
        old = self.make("nzt", "old", hours_old=3)
        lines = janitor.run(self.host, dry_run=True)
        self.assertTrue(os.path.exists(old))
        self.assertTrue(lines[0].startswith("would tidy"))


if __name__ == "__main__":
    unittest.main()


class Zombies(Machine):
    def test_tidy_reports_the_zombies_each_container_holds(self):
        self.docker.containers = {"agentdb-a": (0.1, 1, 5000), "agentdb-b": (0.1, 1, 5001)}
        self.docker.states = {"agentdb-a": ["Ss", "Z", "Z+"], "agentdb-b": ["Ss"]}
        self.assertEqual(janitor.run(self.host), ["agentdb-a holds 2 zombie processes"])


class Networks(Machine):
    def tidy_networks(self, batch_pid):
        self.docker.networks = {"nzf-manual-5-r0-rust": ("nzf-manual-5-r0-rust-service",)}
        self.host.state = {"batches": {"manual-5": {"status": "running", "pid": batch_pid}}} if batch_pid is not None else {}
        janitor.run(self.host)
        return self.docker.issued("docker", "rm") + self.docker.issued("docker", "network", "rm")

    def test_a_network_of_a_batch_whose_process_is_dead_goes_with_its_container(self):
        gone = max(int(p) for p in os.listdir("/proc") if p.isdigit()) + 100000
        self.assertEqual(self.tidy_networks(gone), [["docker", "rm", "-f", "nzf-manual-5-r0-rust-service"],
                                                    ["docker", "network", "rm", "nzf-manual-5-r0-rust"]])

    def test_a_network_of_a_batch_whose_process_is_alive_stays(self):
        self.assertEqual(self.tidy_networks(os.getpid()), [])

    def test_a_network_of_a_batch_nobody_recorded_goes(self):
        self.assertEqual(len(self.tidy_networks(None)), 2)

    def test_docker_networks_that_are_not_a_batchs_are_not_looked_at(self):
        self.docker.networks = {}
        janitor.run(self.host)
        self.assertEqual(self.docker.issued("docker", "network", "inspect"), [])


class WarmCaches(unittest.TestCase):
    def test_every_slot_with_a_lock_file_gets_its_build_directory_capped_and_the_shared_caches_too(self):
        warm = tempfile.mkdtemp()
        for name in ("slot-1.lock", "slot-2.lock", "other"):
            open(os.path.join(warm, name), "w").close()
        found = janitor.caches("/h", warm, "/h/sccache", "/h/cache", "/work")
        self.assertEqual([c[0] for c in found], ["/h/sccache", "/h/cache/npm", "/h/cache/cargo-registry",
                                                 "/work/.rescued", os.path.join(warm, "slot-1", "target"),
                                                 os.path.join(warm, "slot-2", "target")])
        self.assertTrue(all(cap > 0 for _, cap, _ in found))

    def test_a_slot_whose_lock_a_batch_holds_is_held_and_one_nobody_holds_is_not(self):
        warm = tempfile.mkdtemp()
        os.makedirs(os.path.join(warm, "slot-1", "target"))
        lock = os.path.join(warm, "slot-1.lock")
        with open(lock, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertTrue(janitor._locked(lock))
        self.assertFalse(janitor._locked(lock))

    def test_the_tidy_pass_removes_a_cache_over_its_cap(self):
        base = tempfile.mkdtemp()
        cache = os.path.join(base, "sccache")
        os.makedirs(cache)
        open(os.path.join(cache, "blob"), "wb").close()
        host = janitor.Host([], base, base, proc=tempfile.mkdtemp(), run=lambda argv: (0, ""), caches=((cache, 0, ""),))
        with mock.patch.object(janitor, "_gb", return_value=5):
            lines = janitor.run(host)
        self.assertFalse(os.path.exists(cache))
        self.assertTrue(any("over its 0 GB" in line for line in lines), lines)
