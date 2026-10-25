"""git and GitHub, as thin calls. Every function here runs a command and parses its output; none decides anything."""

from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from urllib.parse import urlsplit

import datetime

from . import dispatch
from .selection import areas as selection
from .config import FORK, OWNER, REPO, UPSTREAM


def _main_remote() -> str:
    """The remote that carries main: `upstream` on a fork checkout, `origin` where only origin exists, as in ci."""
    done = subprocess.run(["git", "remote"], cwd=REPO, capture_output=True, text=True)
    remotes = done.stdout.split() if done.returncode == 0 else []
    return "origin" if remotes and "upstream" not in remotes else "upstream"


MAIN_REMOTE = _main_remote()
MAIN = f"{MAIN_REMOTE}/main"
DEPLOY_GATE = "Confirm successful main push"


class Failed(Exception):
    pass


def run(*args, cwd=None, input=None, check=True) -> str:
    done = subprocess.run(args, cwd=cwd or REPO, input=input, capture_output=True, text=True, timeout=600)
    if check and done.returncode != 0:
        raise Failed(f"{' '.join(args[:4])}: {done.stderr.strip()[:300]}")
    return done.stdout


def git(*args, **kw) -> str:
    return run("git", *args, **kw)


def git_bytes(*args, cwd=None, check=True) -> bytes:
    """Git's stdout as bytes. A diff can hold bytes that are not UTF-8, and decoding them would make distinct changes compare equal."""
    done = subprocess.run(["git", *args], cwd=cwd or REPO, capture_output=True, timeout=600)
    if check and done.returncode != 0:
        raise Failed(f"git {' '.join(args[:4])}: {done.stderr.decode('utf-8', errors='replace').strip()[:300]}")
    return done.stdout


def patch_ids(diff: bytes, cwd=None) -> str:
    """`git patch-id` over a byte stream. Its output is hex, so reading it as ASCII loses nothing."""
    done = subprocess.run(["git", "patch-id", "--stable"], cwd=cwd or REPO, input=diff, capture_output=True, timeout=600)
    if done.returncode != 0:
        raise Failed(f"git patch-id: {done.stderr.decode('utf-8', errors='replace').strip()[:300]}")
    return done.stdout.decode("ascii")


def git_ok(*args, cwd=None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd or REPO, capture_output=True).returncode == 0


def gh(*args, **kw) -> str:
    return run("gh", *args, **kw)


def gh_json(*args):
    return json.loads(gh(*args) or "null")


# git

def fetch(numbers) -> None:
    refs = [f"main:refs/remotes/{MAIN}"] + [f"+pull/{n}/head:refs/nzf/pull/{n}" for n in numbers]
    git("fetch", "-q", MAIN_REMOTE, *refs)


def head_of(n: int) -> str:
    return git("rev-parse", f"refs/nzf/pull/{n}").strip()


def main_sha() -> str:
    return git("rev-parse", MAIN).strip()


def origin_repository() -> str:
    """Returns origin's GitHub repository slug, or raises when it cannot be identified safely."""
    url = git("remote", "get-url", "origin").strip()
    ssh = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", url)
    if ssh:
        slug = ssh.group(1)
    else:
        parsed = urlsplit(url)
        if parsed.hostname != "github.com" or parsed.username not in (None, "git") or \
                parsed.port is not None or parsed.query or parsed.fragment:
            raise Failed("origin is not a GitHub repository")
        slug = parsed.path.strip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise Failed("origin is not a GitHub repository")
    return slug


def sync_fork_main() -> bool:
    """Fast-forwards an origin fork from upstream; a diverged fork is left untouched."""
    destination = origin_repository()
    if destination.casefold() == UPSTREAM.casefold():
        return False
    try:
        gh("repo", "sync", destination, "--source", UPSTREAM, "--branch", "main")
    except subprocess.TimeoutExpired as e:
        raise Failed("gh repo sync timed out") from e
    return True


def is_ancestor(a: str, b: str) -> bool:
    return git_ok("merge-base", "--is-ancestor", a, b)


def changed_files(sha: str) -> list:
    return git("diff", "--no-renames", "--name-only", f"{MAIN}...{sha}").split()


def areas(files: list, tier: str = "change") -> frozenset:
    """The areas `files` call for in `tier`: `change` for one pull request, `train` for the merged tree of several."""
    found, _ = selection.select(files, tier)
    return frozenset(found)


def worktree_branches_between(start: float, end: float) -> list:
    """Branches checked out in worktrees that were made in that window."""
    out, path = [], None
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.startswith("branch refs/heads/") and path:
            try:
                made = os.stat(os.path.join(path, ".git")).st_mtime
            except OSError:
                continue
            if start - 5 <= made <= end + 5:
                out.append(line[len("branch refs/heads/"):])
    return out


def worktrees_made_between(start: float, end: float) -> list:
    """Paths of worktrees made in that window, oldest first."""
    out = []
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
            try:
                made = os.stat(os.path.join(path, ".git")).st_mtime
            except OSError:
                continue
            if start - 5 <= made <= end + 5:
                out.append((made, path))
    return [path for _, path in sorted(out)]


def has_merge_commits(sha: str, base: str = MAIN) -> bool:
    return bool(git("rev-list", "--merges", f"{base}..{sha}").strip())


def merged_since(sha: str):
    """PR numbers main has merged since `sha`, oldest first, or None if main took anything but PR merges."""
    if not is_ancestor(sha, MAIN):
        return None
    out = []
    for subject in reversed(git("log", "--first-parent", "--format=%s", f"{sha}..{MAIN}").splitlines()):
        m = re.match(r"Merge pull request #(\d+) ", subject)
        if not m:
            return None
        out.append(int(m.group(1)))
    return out


def merged_prs(limit: int = 500) -> set:
    """Numbers of the PRs main's recent first-parent merge commits name, so a PR merged under another head still counts as merged."""
    subjects = git("log", "--first-parent", f"-n{limit}", "--format=%s", MAIN, check=False).splitlines()
    return {int(m.group(1)) for m in (re.match(r"Merge pull request #(\d+) ", s) for s in subjects) if m}


def undeployed(live: str) -> list:
    """Main's first-parent commits after `live`, oldest first, as (sha, commit time)."""
    lines = git("log", "--first-parent", "--reverse", "--format=%H %ct", f"{live}..{MAIN}").splitlines()
    return [(sha, int(t)) for sha, t in (line.split() for line in lines)]


def merges_cleanly(numbers) -> list:
    """The PRs of `numbers` that do not merge onto main plus the ones before them, in order."""
    tree = main_sha()
    bad = []
    for n in numbers:
        head = head_of(n)
        done = subprocess.run(["git", "merge-tree", "--write-tree", tree, head], cwd=REPO, capture_output=True, text=True)
        if done.returncode != 0:
            bad.append(n)
            continue
        new_tree = done.stdout.split()[0]
        tree = git("commit-tree", new_tree, "-p", tree, "-p", head, "-m", "nzfleet").strip()
    return bad


def verified_tree(members) -> str:
    """The tree main would have after merging each (number, head) in order, or "" when one does not merge."""
    tree = main_sha()
    for _, head in members:
        done = subprocess.run(["git", "merge-tree", "--write-tree", tree, head], cwd=REPO, capture_output=True, text=True)
        if done.returncode != 0:
            return ""
        tree = git("commit-tree", done.stdout.split()[0], "-p", tree, "-p", head, "-m", "nzfleet").strip()
    return git("rev-parse", f"{tree}^{{tree}}").strip()


def main_tree() -> str:
    return git("rev-parse", f"{MAIN}^{{tree}}").strip()


# GitHub

def merge_pr(n: int) -> None:
    gh("pr", "merge", str(n), "-R", UPSTREAM, "--merge")


OPEN_LIMIT = 200


def open_prs() -> list:
    return gh_json("pr", "list", "-R", UPSTREAM, "--state", "open", "--limit", str(OPEN_LIMIT),
                   "--json", "number,headRefOid,headRefName,isDraft,mergeable,updatedAt")


def fork_prs() -> list:
    """The newest open and merged PRs, with the login that owns each head branch and when it last moved."""
    raw = gh_json("pr", "list", "-R", UPSTREAM, "--state", "all", "--limit", "150",
                  "--json", "number,headRefName,headRepositoryOwner,state,updatedAt") or []
    return [{"number": p["number"], "headRefName": p["headRefName"], "state": p["state"],
             "owner": (p.get("headRepositoryOwner") or {}).get("login", ""),
             "updatedAt": datetime.datetime.fromisoformat(p["updatedAt"].replace("Z", "+00:00")).timestamp()} for p in raw]


def create_pr(branch: str, title: str, body: str) -> int:
    """Opens a pull request against main from `branch` on the fork. Returns its number."""
    out = gh("pr", "create", "-R", UPSTREAM, "--head", f"{FORK}:{branch}", "--base", "main", "--title", title, "--body", body)
    return int(out.strip().splitlines()[-1].rsplit("/", 1)[1])


def mark_ready(n: int) -> None:
    gh("pr", "ready", str(n), "-R", UPSTREAM, check=False)


def pr_text(n: int) -> tuple:
    """A PR's title and body."""
    found = gh_json("pr", "view", str(n), "-R", UPSTREAM, "--json", "title,body")
    return found["title"], found["body"] or ""


def branch_of(n: int) -> str:
    return gh_json("pr", "view", str(n), "-R", UPSTREAM, "--json", "headRefName")["headRefName"]


def pull_facts(n: int):
    """What GitHub and the fork say about PR `n`; its head is fetched so one that was force-pushed away reads as gone."""
    found = gh_json("pr", "view", str(n), "-R", UPSTREAM, "--json", "state,headRefOid,headRefName")
    branch, head = found["headRefName"], found["headRefOid"]
    fetch([n])
    return dispatch.PullFacts(found["state"], head, branch,
                              git_ok("ls-remote", "--exit-code", "--heads", "origin", branch),
                              git_ok("cat-file", "-e", f"{head}^{{commit}}"))


def issue(title: str):
    found = gh_json("issue", "list", "-R", UPSTREAM, "--state", "open", "--search", f'in:title "{title}"',
                    "--json", "number,title,body")
    return next((i for i in found or [] if i["title"] == title), None)


def set_issue(title: str, body: str) -> int:
    found = issue(title)
    if found is None:
        url = gh("issue", "create", "-R", UPSTREAM, "--title", title, "--body", body).strip()
        return int(url.rsplit("/", 1)[1])
    if found["body"] != body:
        gh("issue", "edit", str(found["number"]), "-R", UPSTREAM, "--body", body)
    return found["number"]


def close_issue(number: int) -> None:
    gh("issue", "close", str(number), "-R", UPSTREAM)


def pr_comments(n: int) -> tuple:
    """(whether PR `n` is closed or merged, its comment bodies)."""
    found = gh_json("pr", "view", str(n), "-R", UPSTREAM, "--json", "state,comments") or {"state": "OPEN", "comments": []}
    return found["state"] != "OPEN", [c["body"] for c in found["comments"]]


def close_landed(n: int, body: str) -> None:
    gh("pr", "close", str(n), "-R", UPSTREAM, "--comment", body)


@lru_cache(maxsize=8)
def _main_patch_ids(repository: str, main_head: str, depth: int) -> frozenset[str]:
    from .plan import GENERATED_PATHSPECS
    history = git_bytes("log", "-p", "--no-merges", f"-n{depth}", "--format=commit %H", main_head,
                        "--", ".", *GENERATED_PATHSPECS, cwd=repository)
    return frozenset(line.split(" ")[0] for line in patch_ids(history, cwd=repository).splitlines())


def change_on_main(sha: str, depth: int = 400, main_head: str | None = None) -> bool:
    """Whether `sha`'s change, leaving out generated files, is one of main's last `depth` commits, wherever it was merged from."""
    main_head = main_head or main_sha()
    want = change_id(sha, main_head)
    if not want:
        return False
    return want in _main_patch_ids(REPO, main_head, depth)


def comment(number: int, body: str) -> None:
    gh("issue", "comment", str(number), "-R", UPSTREAM, "--body", body)


def ci_jobs(sha: str):
    """The latest CI run on `sha` as (status, {job: conclusion}, run id), or None."""
    runs = gh_json("run", "list", "-R", UPSTREAM, "--workflow", "ci.yaml", "--commit", sha,
                   "--json", "databaseId,status", "--limit", "1")
    if not runs:
        return None
    run_id = runs[0]["databaseId"]
    jobs = gh_json("run", "view", str(run_id), "-R", UPSTREAM, "--json", "jobs")["jobs"]
    return runs[0]["status"], {j["name"]: j["conclusion"] for j in jobs}, run_id


def deploy_runs() -> list:
    """Recent deploy workflow runs, newest first."""
    return gh_json("run", "list", "-R", UPSTREAM, "--workflow", "deploy.yaml",
                   "--json", "databaseId,headSha,status,conclusion", "--limit", "50") or []


def deploy_jobs(run_id: int) -> dict:
    """Conclusions of the jobs in a deploy workflow run."""
    jobs = gh_json("run", "view", str(run_id), "-R", UPSTREAM, "--json", "jobs")["jobs"]
    return {job["name"]: job["conclusion"] for job in jobs}


def job_log(run_id: int, job: str) -> str:
    jobs = gh_json("run", "view", str(run_id), "-R", UPSTREAM, "--json", "jobs")["jobs"]
    job_id = next((j["databaseId"] for j in jobs if j["name"] == job), None)
    if job_id is None:
        return ""
    raw = run("gh", "api", f"repos/{UPSTREAM}/actions/jobs/{job_id}/logs", check=False)
    # Each line starts with a timestamp, and some tools colour their output.
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    return re.sub(r"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", raw)



def owner_comments(since: str) -> list:
    """The owner's comments on issues, PRs and PR diffs since an ISO time, oldest first, as (id, number, body, url)."""
    if not OWNER:
        return []
    out = []
    for path in ("issues/comments", "pulls/comments"):
        for c in gh_json("api", "--paginate", f"repos/{UPSTREAM}/{path}?since={since}&per_page=100") or []:
            if c["user"]["login"] != OWNER:
                continue
            number = int((c.get("issue_url") or c.get("pull_request_url")).rsplit("/", 1)[1])
            out.append((c["id"], number, c["body"], c["html_url"], c["created_at"]))
    return sorted(out, key=lambda c: c[4])


def events(etag: str):
    """The repository's newest events, newest first, and the ETag to send next time. ([], etag) when nothing changed."""
    head = ["-H", f"If-None-Match: {etag}"] if etag else []
    out = run("gh", "api", "-i", *head, f"repos/{UPSTREAM}/events?per_page=30", check=False)
    headers, _, body = out.partition("\r\n\r\n") if "\r\n\r\n" in out else out.partition("\n\n")
    status = headers.splitlines()[0] if headers else ""
    new_tag = next((line.split(":", 1)[1].strip() for line in headers.splitlines() if line.lower().startswith("etag:")), etag)
    if " 304" in status or not body.strip():
        return [], new_tag
    return json.loads(body), new_tag


def change_id(sha: str, base: str) -> str:
    """A fingerprint of what `sha` changes against its merge base with `base`, leaving out generated files,
    so a branch replayed onto a newer main with its screenshots redrawn is still the change that was checked."""
    from .plan import GENERATED_PATHSPECS
    diff = git_bytes("diff", f"{base}...{sha}", "--", ".", *GENERATED_PATHSPECS, check=False)
    return patch_ids(diff).split(" ")[0]


def main_remote() -> str:
    """Main's head on GitHub, read without fetching."""
    return git("ls-remote", "upstream", "refs/heads/main").split()[0]
