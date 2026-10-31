"""`nzfleet train`: the decisions that turn open PRs into one PR, as pure functions of plain data. The git, GitHub and
build effects are in trainrun.py. Not to be confused with train.py, which is a set of PRs a batch verified."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from . import plan, rebases
from .selection import areas, since

SHOT = "shots"
GENERATED = "generated"
SOURCE = "source"
SPEC_COVERAGE = "frontend/admin/e2e/source-coverage.json"


@dataclass(frozen=True)
class Candidate:
    number: int
    files: tuple


def solo_ci(candidates: list) -> list:
    """A change to how checks run is a train of its own: the lowest-numbered one when any is waiting, else everyone."""
    ci = [c for c in candidates if plan.touches_ci(c.files)]
    return ci[:1] or candidates


def ci_proof_areas(ci_sha: str, main_ci: dict, is_ancestor) -> frozenset:
    """The complete batch proof required until main's CI passes the latest pipeline change."""
    passed = main_ci.get("sha")
    if not ci_sha or (main_ci.get("ok") and passed and is_ancestor(ci_sha, passed)):
        return frozenset()
    return frozenset((*areas.ALL, *since.COSTLY, plan.GATE))


CI_NOTICE = ("This changes how checks run, so it is batched alone. Until main's CI passes it, "
             "each following train needs a full batch pass on its exact merged tree.")


def green_at_head(batches: dict) -> frozenset:
    """The (pr, head) pairs a batch of that one pull request alone passed."""
    return frozenset((m[0][0], m[0][1]) for b in batches.values() if b["status"] == "green" and len(m := b["members"]) == 1)


def eligible(open_prs: list, agents: dict, holds: dict, batches: dict) -> list:
    """The open PRs whose task is done, not held, and whose own batch is green at the current head, lowest number
    first. A train's own PR is never one of them."""
    running = {a["pr"]["number"] for a in agents.values() if a.get("status") == "running" and a.get("pr")}
    held = {a["pr"]["number"] for key, a in agents.items() if key in holds and a.get("pr")}
    green = green_at_head(batches)
    return sorted(p["number"] for p in open_prs
                  if not p["headRefName"].startswith("train-") and p["number"] not in running | held
                  and (p["number"], p["headRefOid"]) in green)


def _frontend(files: tuple) -> bool:
    return any(f.startswith("frontend/") for f in files)


def _screens(files: tuple) -> frozenset:
    """The directories of hand-written frontend files, which is what two PRs "touch the same screens" means."""
    return frozenset(posixpath.dirname(f) for f in files if f.startswith("frontend/") and plan.generated_areas([f]) is None)


def order(candidates: list) -> list:
    """Merge order: PRs with no frontend files first, then smaller before larger; a PR sharing screens with one
    already placed follows it directly, so the screens they redraw land next to each other."""
    ranked = sorted(candidates, key=lambda c: (_frontend(c.files), len(c.files), c.number))
    placed: list = []
    while ranked:
        placed.append(ranked.pop(0))
        screens = _screens(placed[-1].files)
        while screens and (next_to := next((c for c in ranked if _screens(c.files) & screens), None)):
            ranked.remove(next_to)
            placed.append(next_to)
            screens = screens | _screens(next_to.files)
    return [c.number for c in placed]


@dataclass(frozen=True)
class Settlement:
    """What to do with one conflicted merge: take the later PR's copy of `take`, then regenerate `regenerate`;
    or, when `by_hand` names files, leave the PR out."""
    take: tuple
    regenerate: tuple
    by_hand: tuple
    keep_main: tuple = ()


def settle(conflicted: list, generators: dict) -> Settlement:
    """Screenshots and files a generator writes are resolved; any other conflicted file needs a person."""
    take, regenerate, by_hand, keep_main = [], [], [], []
    for path in conflicted:
        if path == SPEC_COVERAGE:
            keep_main.append(path)
        elif path in generators:
            take.append(path)
            regenerate.append(path)
        elif path.endswith(".png") and plan.generated_areas([path]) is not None:
            take.append(path)
        else:
            by_hand.append(path)
    return Settlement(tuple(take), tuple(regenerate), tuple(by_hand), tuple(keep_main))


def redraw_areas(files: list) -> list:
    """The areas whose screenshots need drawing again on a tree that changed `files`."""
    return [area for area, prefix in (("console", "frontend/admin/"), ("android", "frontend/android/"))
            if any(f.startswith(prefix) for f in files)]


def drop_breakers(numbers: list, compile_error) -> tuple:
    """Returns (kept, [(pr, error)]): PRs whose merge breaks the build are dropped one at a time. `compile_error(prs)`
    is the error of the merge of exactly `prs` in order, or ''. The first PR whose prefix fails is the one to blame."""
    kept, dropped = list(numbers), []
    while kept and (error := compile_error(kept)):
        low, high = 1, len(kept)  # the prefix of `high` fails
        while low < high:
            mid = (low + high) // 2
            if found := compile_error(kept[:mid]):
                high, error = mid, found
            else:
                low = mid + 1
        dropped.append((kept[high - 1], error))
        kept = kept[:high - 1] + kept[high:]
    return kept, dropped


def title(count: int) -> str:
    return f"{count} open change{'s' if count != 1 else ''} land together"


def shot_screens(changed: list) -> list:
    """The screens named by the shot files a train changed, as `console: 02-dashboard-dark`, sorted and without repeats."""
    names = {"frontend/admin/screenshots/": "console", "frontend/android/docs/screenshots/": "android"}
    return sorted({f"{name}: {posixpath.splitext(path[len(prefix):])[0]}"
                   for path in changed if path.endswith(".png")
                   for prefix, name in names.items() if path.startswith(prefix)})


def body(members: list, resolutions: list, left_out: list, dropped: list, unverified: list, screens: list) -> str:
    """The pull request description: who is in, in order, every automatic resolution and why, who is out and why,
    and what nobody has checked. Each argument is a list of (pr, sentence) except `members`, a list of PR numbers."""
    def section(heading, rows, nothing):
        lines = [f"- #{n}: {why}" for n, why in rows] or [f"- {nothing}"]
        return [f"{heading}", "", *lines, ""]

    return "\n".join([
        "Changes, in merge order:", "", *[f"{i}. #{n}" for i, n in enumerate(members, 1)], "",
        *section("Resolved without a person:", resolutions, "none"),
        *section("Left out, each with a rebase queued behind this pull request:", left_out, "none"),
        *section("Dropped because the merged tree did not compile:", dropped, "none"),
        "Screenshots are refreshed by the batch on the merged tree:", "", *[f"- {s}" for s in screens or ["all affected screens"]], "",
        "Not verified here: tests. The merge check that follows this pull request runs every area over the result; "
        "this change set was only merged and compiled.",
        *[f"Also: {line}" for line in unverified],
    ]).rstrip() + "\n"


def resolution_lines(n: int, settlement: Settlement) -> list:
    rows = []
    shots = [p for p in settlement.take if p not in settlement.regenerate]
    if shots:
        rows.append((n, f"{len(shots)} screenshot(s) conflicted; the later copy was kept for the batch to refresh"))
    for path in settlement.keep_main:
        rows.append((n, f"{path} conflicted; main’s copy was kept for the nightly refresh"))
    for path in settlement.regenerate:
        rows.append((n, f"{path} conflicted; the later copy was kept and its generator ran on the merged tree"))
    return rows


def merge_command(pr: int, upstream: str, head: str) -> str:
    return f"gh pr merge {pr} -R {upstream} --merge --match-head-commit {head}"


def letter(pr: int, members: list, left_out: list, dropped: list, upstream: str, head: str, ci: bool = False) -> tuple:
    """The subject and body of the mail that carries the one command."""
    out = [n for n, _ in left_out] + [n for n, _ in dropped]
    note = f"\nLeft out: {', '.join(f'#{n}' for n in out)}.\n" if out else ""
    return (f"nzela: merge #{pr}, {len(members)} changes",
            f"{title(len(members)).capitalize()}, and the result compiles. Merge it with:\n\n"
            f"{merge_command(pr, upstream, head)}\n{note}\nThe merge check passed on exactly this tree and it has been reviewed.\n"
            + (f"\n{CI_NOTICE}\n" if ci else ""))


def required_areas(files: list, selected: frozenset, skipped: frozenset) -> frozenset:
    """The areas that must have passed on a train: what its files select, and for a frontend train both frontends'
    checks, which hold the full Android unit tests and the two screenshot sets."""
    return plan.check_areas(selected, files, frozenset(), train=True) | frozenset(redraw_areas(files))


def waiting_for(record: dict, passed: dict, batch: dict, head: str, tree: str, required: frozenset) -> str:
    """What stands between a train and its mail, or '' when it may go. `passed` and `batch` are the record of the
    last batch pass for the train's PR and that batch; `tree` is what merging `head` onto main now produces."""
    if record.get("mailed") is not False:
        return "already mailed"
    if not passed or passed["sha"] != head:
        return "its current head has not passed a batch"
    if batch.get("status") != "green":
        return f"its batch is {batch.get('status', 'missing')}, not green"
    if batch.get("skipped") or batch.get("skips"):
        return "its batch skipped or excluded members"
    if batch.get("flaky"):
        return "its batch recorded flaky tests"
    proved = batch.get("proved") or {}
    if proved.get("tree") != tree:
        return "its batch proved a different tree than main would now have"
    missing = sorted(required - set(proved.get("runs", {})))
    if missing:
        return f"no pass on this tree for {', '.join(missing)}"
    if record.get("reviewed") != head:
        return "waiting for review: `nzfleet release-train <pr>`"
    return ""


def left_out_reason(files: list) -> str:
    """Why a member is out: the files it conflicted in, or, with none, that the final merge dropped a change that had compiled."""
    if files:
        return "its changes conflict in " + ", ".join(files)
    return "it compiled in the trial merge but was missing from the final merge"


def rebase_brief(n: int, train_pr: int, files: list) -> str:
    if not files:
        return (f"PR #{n} was left out of #{train_pr}: it compiled in the trial merge but was missing from the final merge. "
                "Rebase it onto upstream/main as plain commits, check that it still merges cleanly with main, "
                "regenerate any screenshots the change alters, run the checks for the areas it touches, "
                "and push with --force-with-lease.")
    return rebases.hands_task(n, {"files": files}) + f" It conflicted while the changes for #{train_pr} were put together."


def fix_brief(n: int, train_pr: int, error: str) -> str:
    return (f"PR #{n} was left out of #{train_pr} because merging it made the build fail:\n\n{error}\n\n"
            "Fix it on its own branch so it compiles on top of main, and push.")


def next_branch(date: str, existing: list) -> str:
    """`train-<date>-<n>` for the first n not yet on the fork."""
    taken = {int(m.group(1)) for b in existing if (m := re.fullmatch(rf"train-{re.escape(date)}-(\d+)", b))}
    return f"train-{date}-{max(taken, default=0) + 1}"


def red_fix_tasks(pr: int, failure: dict) -> list:
    """One fix task per failing area of a train's red verdict, each carrying the log lines that name the failure."""
    return [{"key": f"train-fix-{pr}-{area}", "model": "sonnet", "area": "", "after": [],
             "kind": "train-fix", "pr": pr, "task": f"The merge check of train #{pr} failed in {area}. Fix it on the train's branch (or on the change "
                     f"that caused it) and push.\n\n{failure['why']}\n\n{failure.get('log', '')}"}
            for area in failure["areas"]]


def status_row(pr: str, record: dict, batches: dict) -> dict:
    """What `nzfleet status` shows of a train pull request: membership, batch verdict, and mail gate."""
    ours = [b for b in batches.values() if [n for n, _ in b["members"]] == [int(pr)]]
    verdict = max(ours, key=lambda b: b["started"])["status"] if ours else "not started"
    notice = "mail outcome uncertain" if record.get("mailing") else "sent" if record.get("mailed") else "release requested" if record.get("reviewed") else "review pending"
    return {"pr": int(pr), "members": record["members"], "left_out": record["left_out"], "dropped": record["dropped"],
            "compiled": record["compiled"], "verdict": verdict, "notice": notice}
