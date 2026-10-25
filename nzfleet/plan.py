"""Every decision nzfleet makes, as functions of plain data. Nothing here touches git, GitHub or the disk."""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from . import holds, runs


@dataclass(frozen=True)
class Pr:
    number: int
    sha: str
    areas: frozenset
    draft: bool = False
    conflicting: bool = False
    has_merge_commits: bool = False
    branch: str = ""
    ci: bool = False


@dataclass
class Passed:
    """A head that passed a batch: the batch's main and the order its members merged in."""

    sha: str
    main: str
    order: list
    batch: str


CI_FILES = re.compile(r"^(\.github/workflows/|scripts/checks\.sh$|scripts/areas(/|$)|tools/nzfleet/nzfleet/selection/)")


def touches_ci(files) -> bool:
    """Whether a change edits how checks run, which no check of the old pipeline can vouch for."""
    return any(CI_FILES.match(f) for f in files)


# A group is what a batch shares: PRs in one group are merged together and their
# areas checked once. Android and console checks are slow and touch nothing else.
CI_GROUP = "ci"


def group_of(areas: frozenset) -> str:
    rest = set(areas) - {"secrets"}
    # A PR that touches every area (the check scripts, CI itself) would drag every other core PR through
    # the Android and image builds, so it is checked on its own.
    if {"android", "console", "rust", "image"} <= rest:
        return "wide"
    # Docs, the site and the release scripts check in minutes; they should not wait behind a build.
    if rest <= {"deploy", "site"}:
        return "light"
    if rest and rest <= {"android"}:
        return "android"
    if rest and rest <= {"console", "site"}:
        return "console"
    return "core"


def parents(prs: list, is_ancestor: Callable[[str, str], bool]) -> dict:
    """The PR each PR is stacked on: the nearest other open PR whose head its head contains."""
    out = {}
    for child in prs:
        below = [p for p in prs if p.number != child.number and p.sha != child.sha and is_ancestor(p.sha, child.sha)]
        if below:
            # The nearest is the one every other candidate is below.
            nearest = max(below, key=lambda p: sum(1 for q in below if q.number != p.number and is_ancestor(q.sha, p.sha)))
            out[child.number] = nearest.number
    return out


def root(number: int, up: dict) -> int:
    seen = set()
    while number in up and number not in seen:
        seen.add(number)
        number = up[number]
    return number


def ordered(numbers: list, up: dict) -> list:
    """Parents before children, otherwise by PR number."""
    pending = sorted(numbers)
    out = []
    while pending:
        for n in pending:
            if up.get(n) not in pending:
                out.append(n)
                pending.remove(n)
                break
        else:  # a cycle cannot happen with ancestry, but never loop forever
            out.extend(pending)
            break
    return out


def needs_check(pr: Pr, passed: Optional[Passed], failed_sha: Optional[str], checked: list = ()) -> bool:
    """A head is checked once over every required area, unless it failed and has not moved."""
    if passed is not None and passed.sha == pr.sha and covered(pr.areas, checked):
        return False
    return failed_sha != pr.sha


def batches(waiting: list, up: dict, running: set, slots: int) -> list:
    """Batches to start now: one per group not already running, oldest group first, at most `slots`.

    A stack is grouped by the areas of all its waiting members, so a child rides with its parent.
    """
    stack_areas: dict = {}
    for pr in waiting:
        top = root(pr.number, up)
        stack_areas[top] = stack_areas.get(top, frozenset()) | pr.areas
    by_group: dict = {}
    for pr in waiting:
        by_group.setdefault(CI_GROUP if pr.ci else group_of(stack_areas[root(pr.number, up)]), []).append(pr)
    out = []
    for group in sorted(by_group, key=lambda g: min(p.number for p in by_group[g])):
        if group in running or len(out) >= slots:
            continue
        members = cap(ordered([p.number for p in by_group[group]], up), up)
        members = members[:1] if group == CI_GROUP else members
        chosen = {p.number: p for p in by_group[group] if p.number in members}
        areas = frozenset().union(*(p.areas for p in chosen.values()))
        out.append((group, members, sorted(areas)))
    return out


# Agents build many PRs in parallel against the same files; past six, a batch that passes is the exception.
BATCH_SIZE = 6


def cap(members: list, up: dict, size: int = BATCH_SIZE) -> list:
    """The first `size` members in order, taking a stack whole: a child never rides without its parent."""
    out = []
    for n in members:
        if len(out) >= size and up.get(n) not in out:
            continue
        if up.get(n) is not None and up[n] in members and up[n] not in out:
            continue
        out.append(n)
    return out


@dataclass
class Bisect:
    """Prefix probes of a failing batch: `results[k]` is whether the first k members passed."""

    members: list
    results: dict = field(default_factory=dict)


def next_probe(b: Bisect):
    """('probe', k), ('culprit', member) or ('main-broken', None)."""
    n = len(b.members)
    fails = [k for k, ok in b.results.items() if not ok]
    passes = [k for k, ok in b.results.items() if ok]
    hi = min(fails) if fails else n
    lo = max([k for k in passes if k < hi], default=None)
    if hi == 0:
        return ("main-broken", None)
    # Until some prefix has passed, main itself is unproven: halving reaches k = 0.
    if lo is None:
        return ("probe", hi // 2)
    if hi - lo == 1:
        return ("culprit", b.members[hi - 1])
    return ("probe", (lo + hi) // 2)


# Files a command draws from the code, by the area whose command draws them. A conflict in these
# alone is settled by drawing them again, not by a person.
GENERATED = {
    "console": re.compile(r"^crates/nzela-wire/|^frontend/admin/(screenshots/|docs/comparison\.html$)"),
    "android": re.compile(r"^frontend/android/docs/screenshots/"),
}


GENERATED_PATHSPECS = [":(exclude)frontend/admin/screenshots", ":(exclude)frontend/admin/docs/comparison.html",
                       ":(exclude)frontend/android/docs/screenshots"]


def counterexample(log: str) -> bool:
    """A property test that found a failing input. A rerun that passes only drew other random cases,
    so this is never a flake."""
    return "minimal failing input" in log


def failed_areas(line: str, areas: list) -> list:
    """The areas a checks.sh summary line names as failed, or all of `areas` when it names none,
    since a run that could not say what failed proves nothing about any of them."""
    named = line.split("failed:", 1)[1].split() if "failed:" in line else []
    return [a for a in areas if a in named] or list(areas)


def summary_line(log: str, areas: list) -> str:
    """checks.sh's own closing line: the last one that is `passed:` or `failed:` followed only by area names.
    A test that prints `failed: <its name>` is not one, so it cannot stand in for the summary."""
    named = "|".join(re.escape(a) for a in areas)
    summary = re.compile(rf"(?:passed|failed): (?:{named})(?: (?:{named}))*")
    plain = (re.sub(r"\x1b\[[0-9;]*m", "", line) for line in log.splitlines())
    return next((line for line in reversed(list(plain)) if summary.fullmatch(line)), "")


def generated_areas(files: list) -> Optional[frozenset]:
    """The areas that redraw `files`, or None when any of them is written by hand."""
    areas = set()
    for f in files:
        area = next((a for a, pattern in GENERATED.items() if pattern.match(f)), None)
        if area is None:
            return None
        areas.add(area)
    return frozenset(areas) if areas else None


def covered(touched: frozenset, checked: list, skipped: frozenset = frozenset()) -> bool:
    """A pass counts only when its batch ran every area the PR touches, apart from those left to main's ci."""
    return set(touched) - set(skipped) <= set(checked)


# What draws, compares or publishes the frontends' screenshots and goldens. the area selection sees a workflow or a
# script here as a deploy change, so a pass that ran only that proves nothing about the pixels.
PIXEL_FILES = re.compile(r"(^|/)(golden|goldens|screenshots)(/|$)|^scripts/shot-report|^\.github/workflows/shots\.yaml$")


# What each slow area reads. the area selection also selects them for a change to checks.sh or ci, which rewrites no
# pixel and no fuzz input; a train that touches none of these has nothing for the area to redraw or run.
AREA_INPUTS = {
    "console": re.compile(r"^crates/nzela-wire/|^frontend/admin/|^crates/nzela-(platform|api|web)/src/|^crates/nzela-operator-[a-z]+/src/|^scripts/shots?\.sh$|" + PIXEL_FILES.pattern),
    "console-crawl": re.compile(r"^crates/nzela-wire/|^frontend/admin/|^crates/(nzela-operator-[a-z]+|nzela-web)/src/|^scripts/(contract\.sh|typeshare\.toml)$"),
    "android": re.compile(r"^frontend/android/|^crates/nzela-(platform|api|web)/src/|" + PIXEL_FILES.pattern),
    "fuzz": re.compile(r"^crates/|^fuzz/|^Cargo\.(toml|lock)$"),
}


# What a train's client suites read. These are the area selection's android_reads and console_reads; a test runs
# both over the same paths.
TRAIN_INPUTS = {
    "console": re.compile(r"^crates/nzela-wire/|^frontend/admin/|^crates/(nzela-operator-[a-z]+|nzela-web)/src/|^scripts/(contract\.sh|typeshare\.toml)$"),
    "console-crawl": re.compile(r"^crates/nzela-wire/|^frontend/admin/|^crates/(nzela-operator-[a-z]+|nzela-web)/src/|^scripts/(contract\.sh|typeshare\.toml)$"),
    "android": re.compile(r"^crates/nzela-wire/|^frontend/android/|^crates/nzela-api/src/|^crates/nzela-web/src/code\.rs$|"
                          r"^scripts/(contract\.sh|typeshare\.toml)$"),
}


def check_areas(selected: frozenset, files: list, skipped: frozenset, train: bool = False) -> frozenset:
    """The checks needed before readiness; formal runs after merge, while a train holds clients to its server.

    A train defers screenshot sweeps to main, so a screenshot asks only the suite of the frontend it sits in; what
    `selected` names (compile included) is what the area selection asked for.
    """
    required = set(selected) - set(skipped) - {"formal"}
    if train:
        required.add(GATE)
    if not train and any(PIXEL_FILES.search(file) for file in files):
        required |= {"console", "android"} - set(skipped)
    if not train and "formal" not in skipped and any(file.endswith(".tla") for file in files):
        required.add("formal")
    for area, inputs in (TRAIN_INPUTS if train else AREA_INPUTS).items():
        if not any(inputs.search(file) for file in files):
            required.discard(area)
    return frozenset(required)


def new_batch_id(group: str, now: float, taken) -> str:
    """A batch id no recorded batch has: two batches started in one second would otherwise share a record."""
    base = f"{group}-{int(now)}"
    return next(bid for bid in (base if k == 0 else f"{base}-{k}" for k in itertools.count()) if bid not in taken)


def reruns_on_red(batches: dict, this: str) -> bool:
    """Whether a red batch may be run again and bisected on its own. A manual batch was chosen by a person, who reads
    the failure; a batch is not repeated while a newer running one covers any of its PRs."""
    batch = batches[this]
    if batch.get("group") == "manual":
        return False
    mine = {int(n) for n, _ in batch["members"]}
    return not any(bid != this and b["status"] == "running" and b["started"] > batch["started"]
                   and mine & {int(n) for n, _ in b["members"]} for bid, b in batches.items())


def reusable_batch(batches: dict, members: list, areas: list, main: str, this: str) -> str:
    """The id of an earlier green batch that checked these exact heads, in this order, on this main, over at least
    these areas; empty when there is none."""
    wanted = [[int(n), sha] for n, sha in members]
    for bid, b in batches.items():
        if bid != this and b["status"] == "green" and b["main"] == main and set(areas) <= set(b.get("checked_areas", b["areas"])) \
                and [[int(n), sha] for n, sha in b["members"]] == wanted and b.get("passed") == [n for n, _ in wanted]:
            return bid
    return ""


DAY = 24 * 3600


def stale_shots_step(unowned: bool, main: str, record: dict, now: float) -> str:
    """What to do about shots that moved in a train no change in it owns: `none` when nothing moved or main was
    already redrawn at this commit, `redraw` to draw main alone, `wait` when a refresh was opened less than a day ago."""
    if not unowned or record.get("main") == main:
        return "none"
    return "wait" if now - record.get("opened", 0) < DAY else "redraw"


def still_ready(pr: Pr, passed: Passed, merged_since: Optional[list]) -> bool:
    """A pass holds while the head is the one that passed and main has only taken earlier members of its batch."""
    if passed.sha != pr.sha or pr.conflicting:
        return False
    if merged_since is None:
        return False
    if pr.number not in passed.order:
        return False
    before = set(passed.order[: passed.order.index(pr.number)])
    return set(merged_since) <= before


def queue(ready: dict, started: dict) -> list:
    """Ready PRs in merge order: by when their batch started, then their place in it."""
    return sorted(ready, key=lambda n: (started.get(ready[n].batch, 0), ready[n].order.index(n)))


def grew(before: list, now: list) -> bool:
    return bool(set(now) - set(before))


def land_command(train_id: str) -> str:
    return f"nzfleet land {train_id}" if train_id else ""


# Regular expressions; none of the plain phrases needs escaping.
INFRASTRUCTURE = [
    "No space left on device",
    ": command not found",
    "terminating connection due to administrator command",
    "lease does not exist",
    "is not installed for the toolchain",
    "mount callback failed",
    "not found: content digest sha256:",
    "failed to export layer",
    "No such image: sha256:",
    "address already in use",
    "The operation was canceled",
    "TLC did not finish",
    "UnsatisfiedLinkError",
    "Could not resolve host",
    "received a shutdown signal",
    "fetch of main failed",
    "worktree failed",
    "Terminated",
    "containers failed to start",
    "could not kill container",
    "Error waiting for container",
    "network .* not found",
    "failed to set up container networking",
    "could not find an available, non-overlapping IPv4 address pool",
    "docker could not create the network",
]


def classify(log: str) -> str:
    """'infrastructure' when the log shows the machine failed and no test did, otherwise 'code'.

    A named failing test or a property counterexample is the code, whatever machine noise sits beside it:
    sqlx tests drop their databases, which logs a terminated connection on every run."""
    if failed_tests(log) or "minimal failing input" in log:
        return "code"
    return "infrastructure" if any(re.search(p, log) for p in INFRASTRUCTURE) else "code"


_ERROR_LINE = re.compile(r"error|panicked|FAILED|failed|✘|Traceback", re.I)


def first_error_line(log: str) -> str:
    """The first line of a job log that reports an error, trimmed; 'see the job' when none does."""
    line = next((l.strip() for l in log.splitlines() if _ERROR_LINE.search(l)), "")
    return line[:200] or "see the job"


_TESTS = [
    re.compile(r"^test (\S+) \.\.\. FAILED", re.M),
    re.compile(r"^(\w+ > \w+) FAILED", re.M),
    re.compile(r"✘\s+\d+ \[\w+\] › (.+?) \(\d", re.M),
]


def failed_tests(log: str) -> list:
    out = []
    for pattern in _TESTS:
        out += [m.strip() for m in pattern.findall(log)]
    return sorted(set(out))


FLAKES_TO_FIX = 2


def record_flakes(ledger: dict, tests: list, run: str, now: float) -> dict:
    """The ledger with one more flake for each of `tests` seen in `run`. A run already counted adds nothing."""
    out = {test: {**entry, "runs": list(entry["runs"])} for test, entry in ledger.items()}
    for test in tests:
        entry = out.setdefault(test, {"count": 0, "last_seen": now, "runs": []})
        if run in entry["runs"]:
            continue
        entry.update(count=entry["count"] + 1, last_seen=now)
        entry["runs"].append(run)
    return out


def flake_key(test: str) -> str:
    """The todo key for a test: its name as a slug, with a hash when cutting it short would make two tests collide."""
    slug = re.sub(r"[^a-z0-9]+", "-", test.lower()).strip("-")
    if len(slug) <= 40:
        return f"flake-{slug}"
    return f"flake-{slug[:40].rstrip('-')}-{hashlib.sha1(test.encode()).hexdigest()[:6]}"


def lines_about(log: str, test: str, limit: int = 8) -> str:
    """The lines of `log` that name `test`, by its last path segment."""
    name = re.split(r"::| > | › ", test)[-1]
    return "\n".join([line for line in log.splitlines() if name in line][:limit])


def flake_task(test: str, entry: dict, excerpts: list) -> dict:
    shown = "\n\n".join(f"{run}:\n{text}" for run, text in excerpts if text) or "(the logs are gone)"
    brief = (f"The test `{test}` has failed and then passed on rerun {entry['count']} times, so it is flaky.\n\n"
             f"Failing log excerpts:\n{shown}\n\n"
             "Find the race and fix it by waiting on the real condition, never by a longer timeout. "
             "Prove it: run the test 10 times under 4 busy loops and show every run passing. "
             "Open a draft PR with `nzfleet pr`.")
    return {"key": flake_key(test), "task": brief, "model": "sonnet", "after": []}


def flake_tasks(ledger: dict, taken: set, excerpts_of: Callable) -> list:
    """A task for each test that has flaked often enough and has none yet. `taken` holds the todo and agent keys."""
    return [flake_task(test, entry, excerpts_of(test, entry["runs"]))
            for test, entry in sorted(ledger.items())
            if entry["count"] >= FLAKES_TO_FIX and flake_key(test) not in taken]


def flake_rows(ledger: dict) -> list:
    """[(test, count, last_seen)], the most frequent first, then the most recent."""
    rows = [(test, entry["count"], entry["last_seen"]) for test, entry in ledger.items()]
    return sorted(rows, key=lambda r: (-r[1], -r[2], r[0]))


def _one_line(text: str, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def describe(event: dict):
    """One plain sentence for a repository event worth telling someone about, or None."""
    kind, p = event.get("type"), event.get("payload", {})
    who = event.get("actor", {}).get("login", "someone")
    pr = p.get("pull_request") or {}
    issue = p.get("issue") or {}
    if kind == "PullRequestEvent":
        n, title = pr.get("number"), pr.get("title", "")
        if p.get("action") == "closed" and pr.get("merged"):
            return f"merged #{n} {title}"
        if p.get("action") in ("opened", "reopened", "closed", "ready_for_review"):
            return f"{p['action'].replace('_', ' ')} #{n} {title}"
    if kind == "IssueCommentEvent" and p.get("action") == "created":
        where = "PR" if "pull_request" in issue else "issue"
        return f"{who} commented on {where} #{issue.get('number')}: {_one_line((p.get('comment') or {}).get('body', ''))}"
    if kind == "PullRequestReviewCommentEvent" and p.get("action") == "created":
        return f"{who} commented on the diff of #{pr.get('number')}: {_one_line((p.get('comment') or {}).get('body', ''))}"
    if kind == "PullRequestReviewEvent" and p.get("action") == "submitted":
        return f"{who} reviewed #{pr.get('number')}: {(p.get('review') or {}).get('state', '').lower()}"
    if kind == "IssuesEvent" and p.get("action") in ("opened", "closed", "reopened"):
        return f"{p['action']} issue #{issue.get('number')} {issue.get('title', '')}"
    return None


def pr_standing(head: str, passed: dict, merged: bool, closed: bool = False) -> str:
    """Where a PR stands for the work queue: `merged`, `closed` (without landing), `passed` (its current head passed a
    batch), or `waiting`. A closed PR whose change is on main is passed in as merged."""
    if merged:
        return "merged"
    if closed:
        return "closed"
    return "passed" if passed and passed.get("sha") == head else "waiting"


def area_gate(item: dict, todo: list, agents: dict, standing: dict, area_heads: dict | None = None) -> dict:
    """Whether an item with an area may start: `{"go": True, "base": ...}` or `{"go": False, "why": ...}`.

    It waits for the previous item in its area to finish and for that item's PR to pass a batch or merge.
    A PR closed without landing is skipped: the base is main and `skipped` names it. The base is `{"on": "main"}` or `{"on": "pr", "pr": N, "branch": b}`; `standing` maps a PR number to `pr_standing`.
    """
    earlier = [t for t in todo[: todo.index(item)] if t.get("area") == item["area"] and not t.get("now")]
    prev = earlier[-1]["key"] if earlier else (area_heads or {}).get(item.get("area"))
    if prev is None:
        return {"go": True, "base": {"on": "main"}}
    state = runs.run_state(prev, agents)
    if state == "queued":
        return {"go": False, "why": f"behind {prev}, which has not started"}
    if state == "running":
        return {"go": False, "why": f"waiting for {prev} to finish"}
    if state == "resuming":
        return {"go": False, "why": f"waiting for {prev} to be resumed"}
    if state == "stuck":
        return {"go": False, "why": f"{prev} did not finish; a person decides", "person": True}
    problem = runs.pr_problem(prev, agents)
    if problem:
        return {"go": False, "why": problem[0], "person": problem[1]}
    pr = runs.recorded_pr(prev, agents)
    if pr is None:
        return {"go": True, "base": {"on": "main"}}
    where = standing.get(pr["number"], "waiting")
    if where == "merged":
        return {"go": True, "base": {"on": "main"}}
    if where == "passed":
        return {"go": True, "base": {"on": "pr", "pr": pr["number"], "branch": pr["branch"]}}
    if where == "closed":
        return {"go": True, "base": {"on": "main"}, "skipped": pr["number"]}
    return {"go": False, "why": f"waiting on #{pr['number']} to pass a batch"}


def next_work(todo: list, agents: dict, running: int, limit: int, busy: bool, standing: dict | None = None, held: dict | None = None, area_heads: dict | None = None):
    """The queued task to start now, or None: oldest first whose prerequisites finished, while there is room.

    An item with an area also waits for its area and comes back with its `base`; other areas are not held up by it.
    """
    if busy:
        return None
    done = {key for key, a in agents.items() if a.get("status") == "done"}
    # A run that was resumed finishes under its own key; the task it continues is finished with it.
    done |= {runs.base_key(key) for key in done}
    for item in todo:
        if item.get("now") and item["key"] not in agents and holds.may_start(item, done, standing or {}, held or {}):
            return {**item, "base": {"on": "main"}} if item.get("area") else item
    if running >= limit:
        return None
    for item in todo:
        if item["key"] in agents or not holds.may_start(item, done, standing or {}, held or {}):
            continue
        if not item.get("area"):
            return item
        gate = area_gate(item, todo, agents, standing or {}, area_heads)
        if gate["go"]:
            return {**item, "base": gate["base"], **({"skipped": gate["skipped"]} if "skipped" in gate else {})}
    return None


def is_conflicting(mergeable: str, was: bool) -> bool:
    """GitHub's verdict on whether a PR merges; UNKNOWN is it still computing, so the last answer stands."""
    if mergeable == "CONFLICTING":
        return True
    if mergeable == "MERGEABLE":
        return False
    return was


def stacked_task(item: dict) -> str:
    """The item's brief, followed by what to build on when it has an area."""
    base = item.get("base")
    if not base:
        return item["task"]
    if base["on"] == "main":
        how = "Branch from upstream/main (`nzfleet start <slug>`)."
    else:
        n = base["pr"]
        how = (f"Build on PR #{n}, which has passed its batch but has not merged: `nzfleet start <slug> --base {n}` "
               f"(its branch is {base['branch']}). Open your PR against main and say in its body that it stacks on #{n}.")
    return f"{item['task']}\n\nArea {item['area']}: one agent at a time works here. {how}"


def area_report(todo: list, agents: dict, standing: dict, area_heads: dict) -> dict:
    """For each area, one line per item in queue order: what is running, what waits and on what."""
    out: dict = {}
    for item in todo:
        area = item.get("area")
        if not area:
            continue
        key, state = item["key"], runs.run_state(item["key"], agents)
        if state == "queued":
            gate = area_gate(item, todo, agents, standing, area_heads)
            line = "queued, ready to start" if gate["go"] else f"queued, {gate['why']}"
        elif state == "done":
            problem = runs.pr_problem(key, agents)
            pr = runs.recorded_pr(key, agents)
            line = f"done, {problem[0]}" if problem else \
                f"done, PR #{pr['number']} {standing.get(pr['number'], 'waiting')}" if pr else "done, no PR to wait for"
        else:
            line = state
        out.setdefault(area, []).append(f"{key}: {line}")
    return out


_STATED = re.compile(r"^PR: (?:#(\d+)|(none))\s*$", re.M)


def stated_pr(log: str):
    """What the agent's last `PR: #<n>` or `PR: none` line says: the number, "none", or None when it wrote no such line."""
    found = _STATED.findall(log)
    if not found:
        return None
    number, none = found[-1]
    return "none" if none else int(number)


def find_pr(branches: list, log: str, prs: list, started: float, fork: str):
    """The PR an agent opened or updated, as {number, branch}, or None when it cannot be told.

    `prs` are PRs with number, headRefName, owner, state and updatedAt (epoch seconds). One whose branch the agent
    checked out or named in its log wins, an open one before a merged one; failing that, one updated since the agent
    started that the log names as #N. Two candidates are told apart by the log naming one; otherwise there is no answer.
    """
    mine = [p for p in prs if p["owner"] == fork and p["state"] in ("OPEN", "MERGED")]
    named = {int(a or b) for a, b in re.findall(r"#(\d+)|/pull/(\d+)", log)}

    def says(branch: str) -> bool:
        return branch in branches or bool(re.search(rf"(?<![\w.-]){re.escape(branch)}(?![\w.-])", log))

    pool = [p for p in mine if says(p["headRefName"])] or [p for p in mine if p["updatedAt"] >= started and p["number"] in named]
    for state in ("OPEN", "MERGED"):
        found = [p for p in pool if p["state"] == state]
        if not found:
            continue
        found = found if len(found) == 1 else [p for p in found if p["number"] in named]
        return {"number": found[0]["number"], "branch": found[0]["headRefName"]} if len(found) == 1 else None
    return None


# The agent kit: what `nzfleet start`, `prove`, `wait`, `run` and `pr` decide.

NOTE_INSTRUCTION = ("At each change of phase run `nzfleet note {key} \"<phase>: <one line>\"`, where phase is one of read, "
                    "write, test, fix, push. Prefer one starved run of the touched test class, never the whole suite. "
                    "For that run use `nzfleet starve -- <cmd>`; "
                    "never free-run `yes`, `stress` or `stress-ng`: load generation goes only through `nzfleet starve`, a hook "
                    "refuses it elsewhere, and nzfleet stops any other burner after 10 minutes.")

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def slug_problem(slug: str) -> str:
    """Why `slug` cannot name a branch and a directory, or an empty string."""
    if not 3 <= len(slug) <= 80 or not SLUG.match(slug):
        return "a slug is 3 to 80 characters of lowercase letters, digits and single dashes, e.g. a-held-rate-can-be-discarded"
    return ""


PORT_LOW, PORT_SIZE, PORT_BLOCKS = 20000, 20, 600  # below the kernel's ephemeral range, which starts at 32768


def allocate_ports(taken: set, busy: Callable[[int], bool]) -> Optional[int]:
    """The first port of a block of PORT_SIZE that no other worktree holds and nothing listens on, or None."""
    for i in range(PORT_BLOCKS):
        base = PORT_LOW + i * PORT_SIZE
        if base not in taken and not any(busy(base + k) for k in range(PORT_SIZE)):
            return base
    return None


def migration_order(paths: list) -> list:
    """Every crate's migrations in the order they were written: by file name, not by crate."""
    return sorted(paths, key=lambda p: (p.rsplit("/", 1)[-1], p))


def lock_key(lockfile: bytes) -> str:
    """Names the node_modules a package-lock.json produces."""
    return hashlib.sha256(lockfile).hexdigest()[:16]


def merge_path(extra: str, current: str) -> str:
    """`current` with the directories of `extra` in front of it, each once."""
    seen, out = set(), []
    for part in (extra + ":" + current).split(":"):
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return ":".join(out)


def node_cache_state(ready: bool, installing: bool) -> str:
    return "ready" if ready else "building" if installing else "absent"


def env_file(values: dict) -> str:
    """A file a shell can source."""
    return "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in values.items())


def parse_env(text: str) -> dict:
    """The inverse of env_file; anything that is not an export line is skipped."""
    out = {}
    for line in text.splitlines():
        if line.startswith("export ") and "=" in line:
            key, _, value = line[7:].partition("=")
            parts = shlex.split(value)
            out[key] = parts[0] if parts else ""
    return out


@dataclass(frozen=True)
class Check:
    name: str
    cmd: tuple
    cwd: str = ""        # relative to the worktree
    needs: str = ""      # "db" (DATABASE_URL), "preview" (a running vite preview on the worktree's port) or ""
    slot: bool = False   # run under the machine's Android build slot


CARGO = ("cargo", "+1.98")
ADMIN, ANDROID = "frontend/admin", "frontend/android"
_WORKSPACE_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml", "clippy.toml", "rustfmt.toml", ".rustfmt.toml")


_DEPLOY_PROOFS = {
    "scripts/areas/deploy.sh": ("scripts/checks.sh", "deploy"),
    "scripts/workflow-shells-test.sh": ("scripts/workflow-shells-test.sh",),
    "scripts/workflow-shells-diagnostics-test.sh": ("scripts/workflow-shells-diagnostics-test.sh",),
    "formal/tla/MAPPING.md": ("python3", "scripts/tla-mapping-refs.py"),
}
_PLANNER_TEST_MODULES = ("tests.test_plan", "tests.test_kit", "tests.test_shells")
_PLANNER_FILES = {"tools/nzfleet/nzfleet/plan.py"} | {
    f"tools/nzfleet/{module.replace('.', '/')}.py" for module in _PLANNER_TEST_MODULES
}
_WORKFLOW_SHELL_FILES = {path for path in _DEPLOY_PROOFS if path.startswith("scripts/workflow-shells")}


def _known_change(path: str, tracked: set) -> bool:
    if path in _DEPLOY_PROOFS:
        return True
    if path == ".gitignore":
        return True
    if path.startswith(("docs/", f"{ADMIN}/docs/", f"{ADMIN}/screenshots/", f"{ANDROID}/docs/screenshots/")):
        return True
    if path in _WORKSPACE_FILES or path.startswith(".cargo/"):
        return True
    if path.startswith("crates/"):
        crate = path.split("/", 2)[1]
        return f"crates/{crate}/Cargo.toml" in tracked
    if path.startswith(f"{ADMIN}/"):
        return bool(_CONSOLE_CODE.match(path) or _CONSOLE_PIXELS.match(path) or _SPEC.match(path)
                    or path.startswith(f"{ADMIN}/e2e/") or path.startswith(f"{ADMIN}/docs/mocks/"))
    if path.startswith(f"{ANDROID}/") or path.startswith("tools/nzfleet/"):
        return True
    return False


def _rust_checks(files: list, tracked: set) -> list:
    crates, whole, modules, suites = set(), set(), {}, {}
    everything = False
    for f in files:
        if f in _WORKSPACE_FILES or f.startswith(".cargo/"):
            everything = True
            continue
        m = re.match(r"^crates/([^/]+)/(.+)$", f)
        if not m or f"crates/{m.group(1)}/Cargo.toml" not in tracked:
            continue
        crate, rest = m.groups()
        crates.add(crate)
        if rest.startswith("src/") and rest.endswith(".rs") and rest not in ("src/lib.rs", "src/main.rs"):
            parts = rest[4:-3].split("/")
            if parts[-1] == "mod":
                parts.pop()
            modules.setdefault(crate, set()).add("::".join(parts) + "::")
        elif rest.startswith("tests/") and rest.endswith(".rs"):
            suites.setdefault(crate, set()).add(rest[6:].split("/")[0].removesuffix(".rs"))
        else:
            whole.add(crate)
    if not crates and not everything:
        return []
    scope = ("--workspace",) if everything else tuple(a for c in sorted(crates) for a in ("-p", c))
    out = [Check("rust fmt", (*CARGO, "fmt", "--all", "--check")),
           Check("rust clippy", (*CARGO, "clippy", "--locked", *scope, "--all-targets"), needs="db")]
    if everything:
        return out + [Check("rust tests", (*CARGO, "test", "--locked", "--workspace"), needs="db")]
    for crate in sorted(crates):
        if crate in whole:
            out.append(Check(f"rust tests: {crate}", (*CARGO, "test", "--locked", "-p", crate), needs="db"))
            continue
        if crate in modules:
            target = "--lib" if f"crates/{crate}/src/lib.rs" in tracked else "--bins"
            out.append(Check(f"rust tests: {crate} {' '.join(sorted(modules[crate]))}",
                             (*CARGO, "test", "--locked", "-p", crate, target, "--", *sorted(modules[crate])), needs="db"))
        if crate in suites:
            tests = tuple(a for t in sorted(suites[crate]) for a in ("--test", t))
            out.append(Check(f"rust tests: {crate} {' '.join(sorted(suites[crate]))}",
                             (*CARGO, "test", "--locked", "-p", crate, *tests), needs="db"))
    return out


_SPEC = re.compile(rf"^{ADMIN}/e2e/([^/]+\.spec\.ts)$")
_CONSOLE_CODE = re.compile(rf"^{ADMIN}/(src/|tests/|package(-lock)?\.json$|tsconfig[^/]*\.json$|vite\.config|index\.html$)")
_CONSOLE_PIXELS = re.compile(rf"^{ADMIN}/(src/|public/|index\.html$|scripts/screenshots\.mjs$)")
_CONSOLE_DEPENDENCIES = re.compile(rf"^{ADMIN}/package(-lock)?\.json$")


def _console_checks(files: list, tracked: set) -> list:
    code = any(_CONSOLE_CODE.match(f) for f in files)
    specs = {m.group(1) for f in files if (m := _SPEC.match(f))}
    if any(f.startswith(f"{ADMIN}/e2e/") and not _SPEC.match(f) for f in files):
        specs |= {m.group(1) for f in tracked if (m := _SPEC.match(f))}  # a shared helper: every spec uses it
    shots = any(_CONSOLE_PIXELS.match(f) for f in files)
    out = []
    if code:
        out += [Check("console typecheck", ("npm", "run", "typecheck"), ADMIN),
                Check("console unit tests", ("npm", "run", "test:unit"), ADMIN)]
    if specs or shots or any(_CONSOLE_DEPENDENCIES.match(f) for f in files):
        out.append(Check("console build", ("npm", "run", "build"), ADMIN))
    if specs:
        out.append(Check("console specs: " + " ".join(sorted(specs)),
                         ("npx", "playwright", "test", *(f"e2e/{s}" for s in sorted(specs))), ADMIN, needs="preview"))
    if shots:
        out.append(Check("console screenshots", ("npm", "run", "shots"), ADMIN, needs="preview"))
    return out


_MODULE = r"(?P<module>app|core/[a-z]+|feature/[a-z]+)"
_ANDROID_TEST = re.compile(rf"^{ANDROID}/{_MODULE}/src/(?:test|sharedTest)/java/(?P<package>.+)/(?P<name>[^/]+Test)\.kt$")
_ANDROID_MAIN = re.compile(rf"^{ANDROID}/{_MODULE}/src/main/java/.+/(?P<name>[^/]+)\.kt$")
_ANDROID_PIXELS = re.compile(rf"^{ANDROID}/{_MODULE}/src/(main|test|sharedTest)/")
_ANDROID_SHARED = re.compile(rf"^{ANDROID}/(?:build-logic/|gradle/wrapper/|gradle/libs\.versions\.toml$|gradle/dependency-locks/|(?:[^/]+/)*(?:settings|build)\.gradle(?:\.kts)?$|gradle\.properties$|gradle\.lockfile$)")


def _android_task(module: str) -> str:
    """The Gradle task that runs a module's unit tests: the plain JVM module has `test`, the Android ones the debug variant."""
    return ":core:model:test" if module == "core/model" else ":" + module.replace("/", ":") + ":testDebugUnitTest"


def _android_checks(files: list, tracked: set) -> list:
    if not any(f.startswith(f"{ANDROID}/") for f in files):
        return []
    classes = set()
    for f in files:
        if m := _ANDROID_TEST.match(f):
            classes.add((m["module"], m["package"].replace("/", ".") + "." + m["name"]))
        elif m := _ANDROID_MAIN.match(f):
            for t in tracked:
                if (n := _ANDROID_TEST.match(t)) and n["module"] == m["module"] and n["name"].startswith(m["name"]):
                    classes.add((n["module"], n["package"].replace("/", ".") + "." + n["name"]))
    gradle = ("./gradlew", "--console=plain")
    if any(_ANDROID_SHARED.match(f) for f in files):
        out = [Check("android tests: shared build inputs", (*gradle, "testDebugUnitTest", ":core:model:test"), ANDROID, slot=True)]
    elif classes:
        tasks = []
        for module in sorted({m for m, _ in classes}):
            tasks += [_android_task(module)] + [a for m, c in sorted(classes) if m == module for a in ("--tests", c)]
        out = [Check("android tests: " + " ".join(c.rsplit(".", 1)[1] for _, c in sorted(classes)),
                     (*gradle, *tasks), ANDROID, slot=True)]
    else:
        out = [Check("android compile", (*gradle, ":app:compileDebugKotlin"), ANDROID, slot=True)]
    if any(_ANDROID_PIXELS.match(f) for f in files):
        out.append(Check("android screenshots", (*gradle, "testDebugUnitTest", "-Proborazzi.test.record=true"), ANDROID, slot=True))
    return out


def proof_plan(files: list, tracked: list, base: str) -> list:
    """The checks that prove a change, from the files it touches. Unmapped changes run the complete check script.

    `tracked` is every file in the tree, so a source file can find its tests and a crate can be told from a folder.
    """
    known = set(tracked)
    changed = sorted(f for f in set(files) if not f.startswith(("frontend/admin/screenshots/", "frontend/android/docs/screenshots/")))
    out = [Check("whitespace", ("git", "diff", "--check", base))]
    scripts = sorted(f for f in changed if f.startswith(("scripts/", ".githooks/")) and f.endswith(".sh") and f in known)
    if scripts:
        out.append(Check("shell syntax", ("bash", "-c", 'for f in "$@"; do bash -n "$f" || exit 1; done', "nz", *scripts)))
    if any(not _known_change(path, known) for path in changed):
        out.append(Check("conservative checks", ("scripts/checks.sh", "all"), needs="db"))
        return out
    out.append(Check("secrets", ("scripts/checks.sh", "secrets")))
    if "scripts/areas/deploy.sh" in changed:
        out.append(Check("deploy checks", _DEPLOY_PROOFS["scripts/areas/deploy.sh"]))
    else:
        deploy_files = set(changed) & _DEPLOY_PROOFS.keys()
        if deploy_files & _WORKFLOW_SHELL_FILES:
            deploy_files |= _WORKFLOW_SHELL_FILES
        out += [Check(f"deploy: {path}", _DEPLOY_PROOFS[path]) for path in sorted(deploy_files)]
    out += _rust_checks(changed, known)
    out += _console_checks(changed, known)
    out += _android_checks(changed, known)
    fleet_files = {f for f in changed if f.startswith("tools/nzfleet/")}
    if fleet_files and fleet_files <= _PLANNER_FILES:
        out.append(Check("nzfleet tests: proof planner", ("python3", "-m", "unittest", "-q", *_PLANNER_TEST_MODULES),
                         "tools/nzfleet"))
    elif fleet_files:
        out.append(Check("nzfleet tests", ("bash", "-c", "NZFLEET_ENV=/dev/null NZFLEET_HOME=$(mktemp -d) python3 -m unittest discover -q tests"),
                         "tools/nzfleet"))
    return out


_ERROR = re.compile(r"^(error(\[E\d+\])?[: ]|FAILURE:|e: |.*\bFAILED\b|.*panicked at|.*error TS\d+|.*AssertionError|\s*✘|Error: )")


def first_error(log: str) -> str:
    """The first line of a log that says what went wrong, with the lines after it; the log's last lines if none does."""
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if _ERROR.match(line):
            return "\n".join(lines[i:i + 4])[:600]
    return "\n".join([line for line in lines if line.strip()][-5:])[:600]


def tail(log: str, n: int = 6) -> str:
    return "\n".join([line for line in log.splitlines() if line.strip()][-n:])[-700:]


def proof_problems(proof: Optional[dict], tree: str, dirty: bool) -> list:
    """Why the proof does not cover this commit. `tree` is the tree of HEAD."""
    if proof is None:
        return ["there is no proof. Run: nzfleet prove"]
    if not proof.get("ok"):
        failed = next((c for c in proof.get("checks", []) if not c.get("ok")), {})
        return [f"the last `nzfleet prove` failed at {failed.get('name', 'a check')}: {failed.get('error', '')[:200]}. "
                "Fix it, commit, and run: nzfleet prove"]
    out = []
    if dirty:
        out.append("there are uncommitted changes. Commit them, then run: nzfleet prove")
    if proof.get("tree") != tree:
        out.append("the proof is older than the last commit: the files differ from the ones `nzfleet prove` ran on. Run: nzfleet prove")
    return out


_ATTRIBUTION = [
    (re.compile(r"co-authored-by", re.I), "a Co-Authored-By line"),
    (re.compile(r"generated (?:with|by)\b", re.I), 'a "Generated with" line'),
    (re.compile(r"https?://\S*(?:claude\.ai|anthropic\.com)\S*|https?://\S*session\S*", re.I), "a session link"),
    (re.compile("\U0001F916"), "a robot emoji signature"),
]


def attribution(text: str) -> list:
    """What in `text` credits a tool: the repository's commits and PRs carry none."""
    return [what for pattern, what in _ATTRIBUTION if pattern.search(text)]


def pr_refusals(files: list, commits: list, title: str, description: str, merges: list, proof: Optional[dict],
                tree: str, dirty: bool) -> list:
    """Every reason a pull request cannot be opened yet, each saying what to do. `commits` is (sha, message)."""
    out = []
    if not title.strip() or not description.strip():
        out.append('the title or the description is empty. Run: nzfleet pr "<title>" -m "<what changed and why, a few sentences>"')
    if not files:
        out.append("the branch changes nothing against its base. A draft PR must change files; if you could not do the work, push nothing and say so")
    if merges:
        out.append(f"the branch has merge commits ({', '.join(m[:8] for m in merges)}). Rebuild it as plain commits on its base: "
                   "git rebase --onto <base> <first-own-commit>^ , or cherry-pick your commits onto a fresh `nzfleet start`")
    for sha, message in commits:
        for what in attribution(message):
            out.append(f"commit {sha[:8]} carries {what}. Remove it: git commit --amend (the last commit) or git rebase -i")
    for what in attribution(title + "\n" + description):
        out.append(f"the title or description carries {what}. Remove it")
    return out + proof_problems(proof, tree, dirty)


def build_body(description: str, proof: dict, stacked_on: Optional[int]) -> str:
    """The PR description: what the author says, then what `nzfleet prove` ran and printed."""
    out = [description.strip(), ""]
    if stacked_on:
        out += [f"Stacks on #{stacked_on}.", ""]
    out += ["## Proof", "", f"`nzfleet prove` ran these at {proof['head'][:8]}:", ""]
    for c in proof["checks"]:
        out += [f"**{c['name']}**: {'passed' if c['ok'] else 'failed'} in {c['seconds']}s", "", "```", "$ " + " ".join(c["cmd"]), c.get("output", ""), "```", ""]
    if not proof["checks"]:
        out.append("No check applies to these files.")
    return "\n".join(out).rstrip() + "\n"


def owns(cwd: str, root: str) -> bool:
    """Whether a process's working directory is inside the worktree `root`."""
    try:
        return os.path.commonpath([os.path.realpath(cwd), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


SILENT_AFTER = 15 * 60
DOWN_TITLE = "nzfleet is down"


def down_reason(active: bool, log_age: Optional[float]) -> Optional[str]:
    """Why the daemon counts as down, or None when it is healthy. `log_age` is seconds since tick.log was written, None if it never was."""
    if not active:
        return "the daemon is not active"
    if log_age is None:
        return "tick.log does not exist"
    if log_age > SILENT_AFTER:
        return f"tick.log has not been written for {int(log_age // 60)} minutes"
    return None


def down_body(reason: str, tail: list) -> str:
    return f"nzfleet is down: {reason}.\n\nLast log lines:\n\n```\n" + "\n".join(tail[-20:]) + "\n```\n"


class Alarm(Enum):
    OPEN = "open"
    UPDATE = "update"
    CLOSE = "close"
    NONE = "none"


@dataclass(frozen=True)
class Alert:
    kind: Alarm
    number: int = 0
    body: str = ""


def down_alert(reason: Optional[str], tail: list, open_issue: Optional[dict]) -> Alert:
    """What to do on GitHub: `open_issue` is the open "nzfleet is down" issue as {number, body}, or None."""
    if reason is None:
        return Alert(Alarm.CLOSE, open_issue["number"]) if open_issue else Alert(Alarm.NONE)
    body = down_body(reason, tail)
    if open_issue is None:
        return Alert(Alarm.OPEN, body=body)
    if open_issue["body"] == body:
        return Alert(Alarm.NONE)
    return Alert(Alarm.UPDATE, open_issue["number"], body)


@dataclass(frozen=True)
class Job:
    """How ci runs one area: inside `image` with `service` beside it, or on the host when `image` is empty."""

    area: str
    image: str = ""
    service: Optional[dict] = None
    env: dict = field(default_factory=dict)
    workdir: str = ""
    steps: tuple = ()
    step_workdirs: tuple = ()
    step_envs: tuple = ()
    cpus: int = 0
    service_cpus: int = 0
    warm_volume: bool = False


# fuzz has no job in ci (the nightly run owns it), so a batch compiles it in the rust job's image and database.
# The area every batch runs first: it compiles each client against the merged server, so a change that touches only
# one side cannot break the other on main.
GATE = "compile"
JOB_OF_AREA = {"rust": "check", "fuzz": "check", "console": "console-browser"}
# Steps of ci that a batch replaces with something of its own: the worktree is mounted where checkout would put it,
# the target directory stands in for the cache, and the image already carries node.
STAND_INS = ("actions/checkout", "Swatinem/rust-cache", "actions/setup-node", "actions/upload-artifact",
             "actions/download-artifact")


def _working_directory(area: str, value: str) -> str:
    if not isinstance(value, str) or "${{" in value or os.path.isabs(value):
        raise ValueError(f"{area}: unsupported working-directory {value!r}")
    path = os.path.normpath(value)
    if path == ".." or path.startswith(".." + os.sep):
        raise ValueError(f"{area}: working-directory leaves the workspace: {value!r}")
    return path


def _self_hosted_cpus(area: str, options: str) -> int:
    match = re.search(r"\$\{\{ vars\.CI_RUNS_ON && '--cpus=(\d+)' \|\| '' \}\}", options)
    if not match or options.count("${{") != 1:
        raise ValueError(f"{area}: unsupported self-hosted CPU options {options!r}")
    return int(match[1])


def _native_service_options(area: str, options: str, cpus: int) -> str:
    expression = f"${{{{ vars.CI_RUNS_ON && '--cpus={cpus}' || '' }}}}"
    if options.count("${{") != 1 or expression not in options:
        raise ValueError(f"{area}: unsupported service CPU options {options!r}")
    return options.replace(expression, "").strip()


# A pull request may name its own warm directory; a batch is never a pull request, so both forms mean the same to it.
WARM_VOLUMES = {"${{ github.event_name != 'pull_request' && (vars.CI_WARM_DIR || (vars.CI_RUNS_ON && format('/srv/fleet/nzfleet/ci-cache/repo-{0}', github.repository_id))) || format('nzela-warm-{0}', github.run_id) }}:/warm",
                "${{ github.event_name != 'pull_request' && (vars.CI_WARM_DIR || (vars.CI_RUNS_ON && format('/srv/fleet/nzfleet/ci-cache/repo-{0}', github.repository_id))) || (vars.CI_PR_WARM_DIR || format('/srv/fleet/nzela-pr/ci-cache/repo-{0}', github.repository_id)) }}:/warm"}


def _native_step_env(area: str, values: dict) -> dict:
    supported = {"CI_WARM_OVERRIDE": {"${{ github.event_name != 'pull_request' && vars.CI_WARM_DIR || '' }}",
                                      "${{ github.event_name != 'pull_request' && vars.CI_WARM_DIR || (github.event_name == 'pull_request' && (vars.CI_PR_WARM_DIR || format('/srv/fleet/nzela-pr/ci-cache/repo-{0}', github.repository_id))) || '' }}"},
                 "CI_SELF_HOSTED": {"${{ github.event_name != 'pull_request' && vars.CI_RUNS_ON || '' }}"}}
    result = {}
    for key, value in values.items():
        if "${{" in str(value):
            # CI splits the crawl across a matrix; a batch area has no job clock, so it runs every shard.
            if key == "NZELA_CRAWL_SHARD" and re.fullmatch(r"\$\{\{ matrix\.shard \}\}/\d+", str(value)):
                continue
            # The Rust job splits into lint, test partitions and fuzz; a batch area runs its whole self, on every test.
            if re.fullmatch(r"\$\{\{ matrix\.(lane|partition) \}\}", str(value)) or key == "NZELA_RUST_SELECT":
                continue
            # One job runs several areas in ci; a batch runs one area per job, so only its own is selected.
            selected = re.fullmatch(r"\$\{\{ needs\.select\.outputs\.([a-z-]+) \}\}", str(value))
            if selected:
                result[key] = "true" if selected[1] == area else "false"
                continue
            if value not in supported.get(key, ()):
                raise ValueError(f"{area}: unsupported step environment {key}={value!r}")
            result[key] = "native-cache" if key == "CI_WARM_OVERRIDE" else "native"
        else:
            result[key] = str(value)
    return result


def _host_resource_env(area: str, env: dict) -> dict:
    supported = {"JAVA_TOOL_OPTIONS": "-XX:ActiveProcessorCount=", "GRADLE_OPTS": "-Dorg.gradle.workers.max="}
    result = {}
    for key, value in env.items():
        if key not in supported:
            if "${{" in str(value):
                continue
            result[key] = str(value)
            continue
        guard = r"(?:vars\.CI_RUNS_ON|\(github\.event_name == 'pull_request' \|\| vars\.CI_RUNS_ON\))"
        match = re.fullmatch(r"\$\{\{ " + guard + r" && '" + re.escape(supported[key]) + r"(\d+)' \|\| '' \}\}", str(value))
        if not match:
            raise ValueError(f"{area}: unsupported {key} resource expression")
        result[key] = f"{supported[key]}{match[1]}"
    if area == "android" and (set(result) & set(supported)) != set(supported):
        raise ValueError("android: missing processor or Gradle worker cap")
    return result


# The batch's checkout already holds the base commit, and its repository is mounted read-only.
FETCH_BASE_STEP = "Fetch selected base"


def job_for(workflow: dict, area: str, expressions: dict) -> Job:
    """The environment ci's job for `area` runs in. `expressions` gives the value of each ci expression a job's env
    uses, by env name; an expression with no value, or a step a batch cannot stand in for, is an error rather than
    a quiet difference from ci."""
    job = workflow.get("jobs", {}).get(JOB_OF_AREA.get(area, area)) or {}
    container = job.get("container")
    if not container:
        if area == "android":
            return Job(area, env=_host_resource_env(area, job.get("env") or {}))
        return Job(area)
    env = {}
    for key, value in (job.get("env") or {}).items():
        value = str(value)
        if "${{" in value:
            if key not in expressions:
                raise ValueError(f"{area}: ci sets {key} from {value}, which a batch has no value for")
            else:
                value = expressions[key]
        env[key] = value
    steps = []
    step_workdirs = []
    step_envs = []
    for step in job.get("steps") or []:
        if step.get("name") == FETCH_BASE_STEP:
            continue
        if "uses" in step:
            if area == "console" and step["uses"].startswith("actions/download-artifact") \
                    and (step.get("with") or {}).get("name") == "console-dist":
                continue
            if not step["uses"].startswith(STAND_INS):
                raise ValueError(f"{area}: ci uses {step['uses']}, which a batch has no stand-in for")
            if step["uses"].startswith("actions/download-artifact"):
                raise ValueError(f"{area}: unsupported artifact download")
        elif "if" in step:
            raise ValueError(f"{area}: ci runs a step only `if` {step['if']}, which a batch does not evaluate")
        else:
            steps.append(step["run"])
            step_workdirs.append(_working_directory(area, step["working-directory"])
                                 if "working-directory" in step else "")
            step_envs.append(_native_step_env(area, step.get("env") or {}))
    if steps and steps[-1] in ("scripts/checks.sh rust", "scripts/checks.sh ${{ matrix.area }}"):
        steps[-1] = f"scripts/checks.sh {area}"
    if area == "console" and steps and steps[-1] == "../../scripts/checks.sh console-browser":
        steps[-1] = "../../scripts/checks.sh console"
    services = list((job.get("services") or {}).items())
    if len(services) > 1:
        raise ValueError(f"{area}: ci has {len(services)} services; a batch starts one")
    service = {"name": services[0][0], **services[0][1]} if services else None
    default_workdir = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", "")
    if not isinstance(container, dict):
        raise ValueError(f"{area}: container CPU options are missing")
    cpus = _self_hosted_cpus(area, container.get("options", ""))
    service_cpus = _self_hosted_cpus(area, service.get("options", "")) if service else 0
    volumes = container.get("volumes") or []
    if volumes and (len(volumes) != 1 or volumes[0] not in WARM_VOLUMES):
        raise ValueError(f"{area}: unsupported container volumes {volumes!r}")
    if service:
        service["options"] = _native_service_options(area, service["options"], service_cpus)
    return Job(area, container["image"], service, env,
               _working_directory(area, default_workdir) if default_workdir else "", tuple(steps), tuple(step_workdirs),
               tuple(step_envs), cpus, service_cpus, bool(volumes))


def service_argv(job: Job, name: str, network: str) -> list:
    """The docker command that starts the job's service on `network`, answering to the name ci gives it."""
    service = job.service or {}
    argv = ["docker", "run", "-d", "--init", "--name", name, "--network", network, "--network-alias", service["name"]]
    for key, value in (service.get("env") or {}).items():
        argv += ["-e", f"{key}={value}"]
    return argv + shlex.split(service.get("options", "")) + [service["image"]] + shlex.split(service.get("command", ""))


def job_argv(job: Job, name: str, network: str, mounts: dict, root: str, owner: str, extra_env: dict) -> list:
    """The docker command that runs the job's steps in order in `root`, mounted at the path it has on the host.
    `mounts` maps host paths to container paths, a value ending in `:ro` read-only. The job runs as root, as ci's
    does, and hands what it wrote back to `owner` ("uid:gid") so the host can clean up."""
    argv = ["docker", "run", "--rm", "--init", "--name", name, "--network", network, "--user", "root",
            "-w", os.path.join(root, job.workdir)]
    for host, inside in {root: root, **mounts}.items():
        argv += ["-v", f"{host}:{inside}"]
    # The worktree is another user's to git, and its repository is mounted read-only.
    env = {**extra_env, **job.env, "CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": root,
           "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "nzela-org/nzela",
           "GITHUB_API_URL": "https://api.github.com", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    target = next((mount for mount in mounts.values() if mount == "/target"), None)
    writable = [mount for mount in mounts.values() if not mount.endswith(":ro") and mount != target]
    if target is not None:
        writable.append(os.path.join(target, job.area))
    handback = " ".join(shlex.quote(p) for p in [root, *writable])
    driver = (f'child=""; runner_files=""; finish() {{ status=$?; trap - EXIT; '
              'if [ -n "$runner_files" ]; then rm -rf -- "$runner_files"; fi; '
              f'chown -R {owner} {handback} || handback=$?; '
              'if [ "$status" -eq 0 ] && [ "${handback:-0}" -ne 0 ]; then status=$handback; fi; exit "$status"; }; '
              'set -m; interrupt() { status=$1; trap - TERM INT; if [ -n "$child" ]; then '
              'kill -TERM -- "-$child" 2>/dev/null || :; wait "$child" 2>/dev/null || :; fi; exit "$status"; }; '
              "trap finish EXIT; trap 'interrupt 143' TERM; trap 'interrupt 130' INT; "
              'runner_files=$(mktemp -d "$PWD/.github-runner.XXXXXX") || exit 1; '
              'export GITHUB_PATH="$runner_files/path" GITHUB_ENV="$runner_files/env" '
              'GITHUB_OUTPUT="$runner_files/output" GITHUB_STEP_SUMMARY="$runner_files/summary"; '
              'export GITHUB_SHA=$(git rev-parse HEAD) || exit 1; '
              'touch "$GITHUB_PATH" "$GITHUB_ENV" "$GITHUB_OUTPUT" "$GITHUB_STEP_SUMMARY"; '
              'for s in "$@"; do : > "$GITHUB_OUTPUT"; : > "$GITHUB_STEP_SUMMARY"; '
              'bash -e -c "$s" & child=$!; wait "$child"; status=$?; child=""; [ "$status" -eq 0 ] || exit "$status"; '
              'while IFS= read -r path || [ -n "$path" ]; do '
              '[ -z "$path" ] || PATH="$path:$PATH"; done < "$GITHUB_PATH"; : > "$GITHUB_PATH"; export PATH; '
              'while IFS= read -r line || [ -n "$line" ]; do '
              '[ -z "$line" ] && continue; '
              'if [[ "$line" == *"<<"* ]]; then key=${line%%<<*}; delimiter=${line#*<<}; value=""; '
              'while IFS= read -r part; do [ "$part" = "$delimiter" ] && break; '
              'value="${value:+$value$\'\\n\'}$part"; done; '
              'else key=${line%%=*}; value=${line#*=}; fi; '
              '[[ "$key" =~ ^[a-zA-Z_][a-zA-Z_0-9]*$ ]] || exit 1; '
              '[[ "$key" == GITHUB_* || "$key" == RUNNER_* ]] && continue; '
              'printf -v "$key" "%s" "$value"; export "$key"; '
              'done < "$GITHUB_ENV"; : > "$GITHUB_ENV"; done')
    steps = []
    for index, step in enumerate(job.steps):
        directory = job.step_workdirs[index] if index < len(job.step_workdirs) else ""
        step_env = job.step_envs[index] if index < len(job.step_envs) else {}
        prefix = f"cd -- {shlex.quote(os.path.join(root, directory))}\n" if directory else ""
        variables = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in step_env.items())
        steps.append(prefix + (variables + "\n" if variables else "") + step)
    return argv + [job.image, "bash", "-c", driver, "nzfleet", *steps]


def summary(wanted: list, failed: list) -> str:
    """The last line of a check run, in the form `failed_areas` reads back."""
    return f"failed: {' '.join(failed)}" if failed else f"passed: {' '.join(wanted)}"
