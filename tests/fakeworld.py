"""A world for driving `tick.run` end to end: a real git repository in a temp directory, a fake GitHub, and a
batch runner that answers from a table. Nothing here touches the network, docker or the machine's own state."""

import atexit
import contextlib
import os
import shutil
import subprocess
import tempfile
import types
from argparse import Namespace
from unittest import mock

from nzfleet import agents, batch, blocked, cli, config, land, machine, mail, shape, snapshot_tick, state, tick, world

OLD = "2020-01-01T00:00:00Z"
SHOT = "frontend/admin/screenshots/a.png"
CODE = "crates/nzela-platform/src/a.rs"


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def real_repo_is_bare():
    """Whether the checkout the suite runs from has been flipped to bare; read before and after every world."""
    return subprocess.run(["git", "config", "--get", "core.bare"], capture_output=True, text=True).stdout.strip() == "true"


_BASE = []


def base_world():
    """The repository and its bare origin as every world starts, built once per process and only ever copied."""
    if not _BASE:
        root = os.path.realpath(tempfile.mkdtemp())
        atexit.register(shutil.rmtree, root, True)
        origin, repo = os.path.join(root, "origin.git"), os.path.join(root, "repo")
        sh(root, "git", "init", "-q", "--bare", origin)
        sh(root, "git", "init", "-q", "-b", "main", repo)
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            sh(repo, "git", "config", key, value)
        sh(repo, "git", "remote", "add", "origin", origin)
        for path in (CODE, SHOT):
            os.makedirs(os.path.dirname(os.path.join(repo, path)), exist_ok=True)
            with open(os.path.join(repo, path), "w") as f:
                f.write("base\n")
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", "base")
        sh(repo, "git", "update-ref", world.MAIN.replace("upstream", "refs/remotes/upstream", 1), "main")
        _BASE.append((root, origin))
    return _BASE[0]


def copy_base(into):
    """A private copy of the base world under `into`, with the repository's origin pointing at the copy."""
    root, origin = base_world()
    shutil.copytree(root, into, symlinks=True, dirs_exist_ok=True)
    config_path = os.path.join(into, "repo", ".git", "config")
    with open(config_path) as f:
        text = f.read()
    with open(config_path, "w") as f:
        f.write(text.replace(origin, os.path.join(into, "origin.git")))


class Runner:
    """Merge-checks that fail with `logs[n]` whenever member n is in the run, and pass otherwise.
    A member in `skips` (PR to its skip record) is left out of the merge, as merge-check leaves out one that conflicts."""

    def __init__(self, logs=None, flaky=None, skips=None):
        self.logs, self.flaky, self.skips, self.calls = logs or {}, flaky, skips or {}, []

    def __call__(self, name, areas, members):
        numbers = [n for n, _ in members if n not in self.skips]
        self.calls.append(numbers)
        if self.flaky and len(self.calls) == 1:
            return self.result(1, numbers, self.flaky, self.skips, areas)
        bad = next((self.logs[n] for n in numbers if n in self.logs), None)
        return self.result(1 if bad else 0, numbers, bad or "passed: rust", self.skips, areas)

    @staticmethod
    def result(rc, merged, log, skips=None, areas=()):
        skips = skips or {}
        return {"rc": rc, "merged": merged, "skipped": sorted(skips), "skips": {str(n): v for n, v in skips.items()},
                "line": log.splitlines()[0], "log": log, "tree": "fake-merged-tree",
                "stats": {area: {"rc": rc} for area in areas}}


class World:
    """Start it with `with World() as w:`; `w.tick()` is one pass of the loop."""

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.was_bare = real_repo_is_bare()
        self.dir = os.path.realpath(tempfile.mkdtemp())
        self.repo, self.origin = os.path.join(self.dir, "repo"), os.path.join(self.dir, "origin.git")
        copy_base(self.dir)
        self.runner, self.deploys, self.ci = Runner(), [], None
        self.prs, self.draft, self.pinned, self.issues = {}, {}, {}, {}
        self.said, self.ready, self.comments, self.logs = [], [], [], {}
        self.pauses = []
        self.closed = {}
        self.mails, self.mail_ok, self.texts, self.branches = [], True, {}, {}
        self.real_work, self.real_supervise = tick.work, tick.supervise
        self.patch_everything()
        return self

    def __exit__(self, *exc):
        self.stack.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        if real_repo_is_bare() and not self.was_bare:
            raise AssertionError("a test turned the real repository bare")

    def patch_everything(self):
        home = os.path.join(self.dir, "home")
        os.makedirs(home)
        patches = [
            mock.patch.object(state, "PATH", os.path.join(home, "state.json")),
            mock.patch.object(state, "LOCK", os.path.join(home, "state.lock")),
            mock.patch.object(config, "AGENT_SLOTS", 4),
            mock.patch.object(config, "refresh"),
            mock.patch.object(batch, "RUNS", os.path.join(home, "runs")),
            mock.patch.object(shape, "WORK", self.dir),
            mock.patch.object(world, "REPO", self.repo),
            mock.patch.object(machine, "busy", return_value=False),
            mock.patch.object(snapshot_tick, "write"),
            mock.patch.object(machine, "scoped", side_effect=lambda kind, argv, **kw: argv),
            mock.patch.object(tick, "say", side_effect=self.said.append),
            mock.patch.object(tick, "tell_event", side_effect=self.said.append),
            mock.patch.object(tick, "work"), mock.patch.object(tick, "tidy"), mock.patch.object(tick, "comments"), mock.patch.object(tick, "supervise"),
            mock.patch.dict(os.environ, {blocked.LIVE: "1"}),
            mock.patch.object(agents, "LOGS", os.path.join(home, "agents")),
            mock.patch.object(agents, "subprocess", types.SimpleNamespace(Popen=self.agent, STDOUT=subprocess.STDOUT)),
            mock.patch.object(tick, "subprocess", types.SimpleNamespace(Popen=self.spawn, DEVNULL=subprocess.DEVNULL)),
            mock.patch.object(batch, "_spawn", side_effect=self.start_batch),
            mock.patch.object(world, "areas", side_effect=self.areas),
            mock.patch.object(world, "fetch", side_effect=self.fetch),
            mock.patch.object(world, "sync_fork_main"),
            mock.patch.object(mail, "send", side_effect=self.send_mail),
            mock.patch.object(world, "pr_text", side_effect=lambda n: self.texts.get(n, (f"PR {n}", ""))),
            mock.patch.object(world, "events", return_value=([], "")),
            mock.patch.object(world, "open_prs", side_effect=self.open_prs),
            mock.patch.object(world, "branch_of", side_effect=lambda n: self.prs[n]),
            mock.patch.object(world, "pull_facts", side_effect=world.Failed("no github in a test")),
            mock.patch.object(world, "mark_ready", side_effect=self.ready.append),
            mock.patch.object(world, "merge_pr", side_effect=self.merge_pr),
            mock.patch.object(land, "pause", side_effect=lambda: self.pauses.append(1)),
            mock.patch.object(world, "issue", side_effect=lambda title: self.issues.get(title)),
            mock.patch.object(world, "set_issue", side_effect=self.set_issue),
            mock.patch.object(world, "pr_comments", side_effect=lambda n: (n in self.closed, self.closed.get(n, []))),
            mock.patch.object(world, "close_landed", side_effect=self.close_pr),
            mock.patch.object(world, "comment", side_effect=lambda n, body: self.comments.append(body)),
            mock.patch.object(world, "ci_jobs", side_effect=lambda sha: self.ci),
            mock.patch.object(world, "job_log", side_effect=lambda run, job: self.logs[job]),
            mock.patch.object(world, "deploy_runs", side_effect=lambda: self.deploys),
            mock.patch.object(world, "deploy_jobs", side_effect=lambda run: self.deploy_jobs[run]),
        ]
        self.deploy_jobs = {}
        for p in patches:
            self.stack.enter_context(p)
        with state.change() as s:
            s["deploy_runs"] = []

    # git

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self, message):
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", message)
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def publish_main(self):
        """What `git fetch upstream` does: the remote-tracking main follows main."""
        sh(self.repo, "git", "update-ref", world.MAIN.replace("upstream", "refs/remotes/upstream", 1), "main")

    def main_sha(self):
        return sh(self.repo, "git", "rev-parse", "main")

    def push_branch(self, number, branch, draft=True):
        sh(self.repo, "git", "push", "-q", "-f", "origin", f"{branch}:refs/heads/{branch}")
        self.prs[number] = self.branches[number] = branch
        self.draft[number] = draft

    def open_pr(self, number, files, base="main", merge=None):
        """A branch of `base` that writes each of `files` and is open as PR `number`; merging `merge` in first."""
        branch = f"pr{number}"
        sh(self.repo, "git", "checkout", "-q", "-B", branch, base)
        if merge:
            sh(self.repo, "git", "merge", "-q", "--no-ff", "-m", f"Merge {merge}", merge)
        for path, text in files.items():
            self.write(path, text)
        sha = self.commit(f"PR {number}")
        self.push_branch(number, branch)
        sh(self.repo, "git", "checkout", "-q", "main")
        return sha

    def merge_pr(self, number):
        """What GitHub's merge button does: a merge commit on main whose subject names the PR."""
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "merge", "-q", "--no-ff", "-m", f"Merge pull request #{number} from fork/pr{number}",
           self.prs[number])
        self.publish_main()
        del self.prs[number]

    def close_pr(self, number, comment=None):
        """Closes PR `number` without merging, optionally leaving `comment` on it."""
        self.closed[number] = [comment] if comment else []
        self.prs.pop(number, None)

    def advance_main(self, files):
        sh(self.repo, "git", "checkout", "-q", "main")
        for path, text in files.items():
            self.write(path, text)
        sha = self.commit("main moves")
        self.publish_main()
        return sha

    def head(self, number):
        return sh(self.origin, "git", "rev-parse", f"refs/heads/{self.prs[number]}")

    # GitHub

    def mergeable(self, number):
        head = self.head(number)
        pinned = self.pinned.get(number)
        if pinned and pinned[0] == head:
            return pinned[1]
        merged = subprocess.run(["git", "merge-tree", "--write-tree", world.MAIN, head], cwd=self.repo, capture_output=True)
        return "MERGEABLE" if merged.returncode == 0 else "CONFLICTING"

    def open_prs(self):
        return [{"number": n, "headRefOid": self.head(n), "headRefName": b, "isDraft": self.draft[n],
                 "mergeable": self.mergeable(n), "updatedAt": OLD} for n, b in sorted(self.prs.items())]

    def fetch(self, numbers):
        for n in numbers:
            sh(self.repo, "git", "fetch", "-q", "origin", f"+refs/heads/{self.branches[n]}:refs/nzf/pull/{n}")

    def send_mail(self, subject, body):
        if self.mail_ok:
            self.mails.append((subject, body))
        return self.mail_ok

    def mailed(self, word):
        """The mails whose subject has `word` in it."""
        return [m for m in self.mails if word in m[0]]

    def set_issue(self, title, body):
        self.issues[title] = {"number": 811, "title": title, "body": body}
        return 811

    def areas(self, files, tier="change"):
        return frozenset({"rust" if f.startswith("crates/") else "console" for f in files})

    def queued(self):
        """The PRs of the train the Merge queue issue names."""
        train = state.read()["trains"].get(self.queue().split("nzfleet land ")[-1].split("\n")[0], {})
        return [n for n, _ in train.get("members", [])]

    def queue(self):
        return self.issues.get(tick.QUEUE_TITLE, {}).get("body", "")

    # the processes the tick starts

    def spawn(self, argv, **kw):
        """Runs a replay or a batch to its end where the daemon would leave it running."""
        if "replay" in argv:
            cli.replay(Namespace(pr=int(argv[-1]), dry_run=False))
        elif "run-batch" in argv:
            batch.run(argv[-1], check=self.runner)
        return types.SimpleNamespace(pid=2**22 + 1)

    def start_batch(self, bid):
        batch.run(bid, check=self.runner)
        return 2**22 + 1

    def agent(self, argv, **kw):
        return types.SimpleNamespace(pid=2**22 + 2)

    def tick(self):
        tick.run()
        return state.read()
