import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
LOCAL_GIT_VARS = subprocess.run(["git", "rev-parse", "--local-env-vars"], check=True,
                                capture_output=True, text=True).stdout.splitlines()


class PrePush(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.clean_env = {key: value for key, value in os.environ.items()
                          if key not in LOCAL_GIT_VARS and key not in ("GIT_NAMESPACE", "NZELA_BASE")}
        self.caller = Path(self.scratch.name) / "caller"
        self.caller.mkdir()
        (self.caller / ".githooks").mkdir()
        (self.caller / "scripts").mkdir()
        (self.caller / "tools/nzfleet/nzfleet").mkdir(parents=True)
        shutil.copy2(ROOT / ".githooks/pre-push", self.caller / ".githooks/pre-push")
        shutil.copy2(ROOT / "scripts/changed-areas.sh", self.caller / "scripts/changed-areas.sh")
        guard = self.caller / "scripts/no-loosened-limits.sh"
        guard.write_text("#!/bin/sh\nprintf called > \"$HOOK_GUARD_MARKER\"\n")
        guard.chmod(0o755)
        checks = self.caller / "scripts/checks.sh"
        checks.write_text("""#!/bin/bash
set -e
git init -q -b main "$HOOK_FIXTURE"
git -C "$HOOK_FIXTURE" remote add origin "$HOOK_ORIGIN"
printf 'fixture\\n' > "$HOOK_FIXTURE/proof.txt"
git -C "$HOOK_FIXTURE" add -A
git -C "$HOOK_FIXTURE" -c user.name=t -c user.email=t@t commit -qm fixture
exit "${HOOK_CHECK_EXIT:-0}"
""")
        checks.chmod(0o755)
        self.git("init", "-q", "-b", "book-ledger-outside", str(self.caller))
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        origin = Path(self.scratch.name) / "origin.git"
        self.git("init", "-q", "--bare", str(origin))
        self.git("remote", "add", "origin", str(origin))
        (self.caller / "tools/nzfleet/nzfleet/plan.py").write_text("base\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("update-ref", "refs/remotes/upstream/main", self.git("rev-parse", "HEAD"))
        (self.caller / "tools/nzfleet/nzfleet/plan.py").write_text("changed\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        self.origin = origin

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.caller), *args], env=self.clean_env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def snapshot(self):
        return (self.git("rev-parse", "HEAD"), self.git("symbolic-ref", "HEAD"),
                self.git("rev-parse", "refs/heads/book-ledger-outside"),
                (self.caller / ".git/index").read_bytes())

    def test_checks_leave_the_callers_head_ref_and_index_untouched_on_pass_and_failure(self):
        before = self.snapshot()
        for outcome, expected in ((0, 0), (42, 1)):
            with self.subTest(check_exit=outcome):
                fixture = Path(self.scratch.name) / f"fixture-{outcome}"
                env = dict(self.clean_env, GIT_DIR=str(self.caller / ".git"), GIT_WORK_TREE=str(self.caller),
                           GIT_COMMON_DIR=str(self.caller / ".git"), GIT_INDEX_FILE=str(self.caller / ".git/index"),
                           GIT_OBJECT_DIRECTORY=str(self.caller / ".git/objects"), GIT_PREFIX="",
                           HOOK_FIXTURE=str(fixture), HOOK_ORIGIN=str(self.origin), HOOK_CHECK_EXIT=str(outcome),
                           NZFLEET_AGENT_KEY="fix-test",
                           HOOK_GUARD_MARKER=str(Path(self.scratch.name) / "guard-called"))
                run = subprocess.run([str(self.caller / ".githooks/pre-push")], cwd=self.caller,
                                     env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
                self.assertEqual(Path(env["HOOK_GUARD_MARKER"]).read_text(), "called")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.git("status", "--porcelain"), "")
                self.assertTrue((fixture / ".git").is_dir())
                built = subprocess.run(["git", "-C", str(fixture), "log", "-1", "--format=%s"],
                                       env=self.clean_env, check=True, capture_output=True, text=True)
                self.assertEqual(built.stdout.strip(), "fixture")

    def test_branch_fixture_uses_its_own_repo_under_hook_environment(self):
        before = self.snapshot()
        env = dict(self.clean_env, GIT_DIR=str(self.caller / ".git"), GIT_WORK_TREE=str(self.caller),
                   GIT_INDEX_FILE=str(self.caller / ".git/index"),
                   GIT_COMMON_DIR=str(self.caller / ".git"), NZFLEET_ENV="/dev/null",
                   NZFLEET_HOME=str(Path(self.scratch.name) / "nzfleet-home"))
        run = subprocess.run(["python3", "-m", "unittest",
                              "tests.test_shells.Branch.test_a_proven_commit_prints_the_pr_it_would_open", "-q"],
                             cwd=ROOT / "tools/nzfleet", env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
