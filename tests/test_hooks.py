import json
import os
import subprocess
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
HOOKS = os.path.join(ROOT, ".claude", "hooks")
ORCHESTRATOR, WORKER = "/srv/fleet/nzela-new", "/srv/fleet/work/some-branch"
KILL_BY_NAME = "pkill " + "-f vite"


def exit_code(hook, event):
    done = subprocess.run(["python3", "-I", os.path.join(HOOKS, hook)], input=json.dumps(event), capture_output=True, text=True)
    return done.returncode


def bash(command, cwd=ORCHESTRATOR, **extra):
    return exit_code("bash-guard.py", {"cwd": cwd, "tool_input": {"command": command, **extra}})


def agent(**tool_input):
    return exit_code("agent-guard.py", {"tool_input": {"prompt": "read the logs", **tool_input}})


class BashGuard(unittest.TestCase):
    def test_sleep_at_a_command_start_is_denied_in_the_orchestrator_checkout(self):
        self.assertEqual(bash("sleep 30"), 2)
        self.assertEqual(bash("make build; sleep 5"), 2)

    def test_sleep_is_allowed_in_a_worker_worktree(self):
        self.assertEqual(bash("sleep 30", cwd=WORKER), 0)

    def test_sleep_in_the_background_is_allowed(self):
        self.assertEqual(bash("sleep 30", run_in_background=True), 0)

    def test_a_polling_loop_is_denied_in_the_orchestrator_checkout(self):
        self.assertEqual(bash("until curl -sf localhost:1; do sleep 2; done"), 2)

    def test_killing_by_name_at_a_command_start_is_denied_everywhere(self):
        self.assertEqual(bash(KILL_BY_NAME), 2)
        self.assertEqual(bash("cd x && " + KILL_BY_NAME, cwd=WORKER), 2)

    def test_killing_by_name_inside_an_argument_is_allowed(self):
        self.assertEqual(bash(f"git commit -m 'never {KILL_BY_NAME}'"), 0)

    def test_an_ordinary_command_is_allowed(self):
        self.assertEqual(bash("git status"), 0)


class AgentGuard(unittest.TestCase):
    def test_a_call_with_no_model_is_denied(self):
        self.assertEqual(agent(subagent_type="general-purpose"), 2)

    def test_opus_is_denied(self):
        self.assertEqual(agent(subagent_type="general-purpose", model="opus"), 2)

    def test_sonnet_for_read_only_work_is_allowed(self):
        self.assertEqual(agent(subagent_type="general-purpose", model="sonnet"), 0)

    def test_a_prompt_that_asks_for_a_pull_request_is_denied(self):
        self.assertEqual(agent(model="sonnet", prompt="fix it and open a PR"), 2)

    def test_explore_is_allowed_without_a_model(self):
        self.assertEqual(agent(subagent_type="Explore"), 0)


class ContextCap(unittest.TestCase):
    def test_a_missing_transcript_is_not_an_error(self):
        self.assertEqual(exit_code("context-cap.py", {"transcript_path": "/nonexistent"}), 0)


class Settings(unittest.TestCase):
    def test_every_hook_the_settings_name_is_in_the_repository(self):
        with open(os.path.join(ROOT, ".claude", "settings.json")) as f:
            settings = json.load(f)
        commands = [h["command"] for group in settings["hooks"].values() for m in group for h in m["hooks"]]
        self.assertEqual(len(commands), 3)
        for command in commands:
            name = command.replace('"$CLAUDE_PROJECT_DIR/.claude/hooks/', "").rstrip('"')
            self.assertTrue(os.access(os.path.join(HOOKS, name), os.X_OK), name)


class WorkerBash(unittest.TestCase):
    HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "worker-bash.py")

    def run_hook(self, **tool_input):
        done = subprocess.run(["python3", "-I", self.HOOK], input=json.dumps({"tool_input": {"command": "make", **tool_input}}),
                              capture_output=True, text=True)
        return done.returncode, done.stderr

    def test_a_background_call_is_denied_and_names_the_alternatives(self):
        code, why = self.run_hook(run_in_background=True)
        self.assertEqual(code, 2)
        self.assertIn("nzfleet wait", why)

    def test_a_foreground_call_is_allowed(self):
        self.assertEqual(self.run_hook(timeout=600000)[0], 0)
        self.assertEqual(self.run_hook(run_in_background=False)[0], 0)

    def command(self, text):
        done = subprocess.run(["python3", "-I", self.HOOK], input=json.dumps({"tool_input": {"command": text}}),
                              capture_output=True, text=True)
        return done.returncode, done.stderr

    def test_every_form_of_git_stash_is_denied_and_names_the_alternative(self):
        for text in ("git stash", "git stash pop", "git -C /w stash list", "git -c user.name=x stash push -- a.rs",
                     "cd w && git stash", "make; git stash", "git log | git stash", "FOO=1 git stash",
                     "/usr/bin/git --no-pager stash", "bash -c 'cargo test && git stash pop'", "echo hi\ngit stash",
                     "(git stash)", "git --git-dir=.git stash"):
            with self.subTest(text):
                code, why = self.command(text)
                self.assertEqual(code, 2)
                self.assertIn("git revert --no-commit", why)
                self.assertIn("shared by every worktree", why)

    def test_a_command_that_only_mentions_stash_is_allowed(self):
        for text in ("grep -rn 'git stash' docs", "git commit -m 'never run git stash here'", "echo git stash",
                     "git log --grep=stash", "git status && git diff", "git stashes", "git checkout -- stash", "git commit -m \"it's\""):
            with self.subTest(text):
                self.assertEqual(self.command(text)[0], 0)
