"""`nzfleet train`: merges open PRs in a scratch worktree, compiles the result, and opens it as one PR.
The decisions are trainpr.py's; this file runs git and GitHub and records what happened."""

from __future__ import annotations

import datetime
import enum
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from . import batch, config, jev, mail, plan, shape, state, trainbuild, trainpr, world


@dataclass
class Assembly:
    """One scratch worktree holding main plus the PRs that merged into it."""
    tree: str
    merged: list = field(default_factory=list)
    left_out: list = field(default_factory=list)   # (pr, conflicted files)
    resolutions: list = field(default_factory=list)  # (pr, sentence)
    regenerate: list = field(default_factory=list)  # paths whose generator runs on the result


def _git(tree: str, *args) -> subprocess.CompletedProcess:
    return shape._git(tree, *args)


def _conflicted(tree: str) -> list:
    return world.git("diff", "--name-only", "--diff-filter=U", cwd=tree).split()


def _must(tree: str, *args) -> None:
    """Runs git in the scratch tree; a failing step stops the train rather than assemble a tree that is not what it says."""
    done = _git(tree, *args)
    if done.returncode != 0:
        raise world.Failed(f"git {' '.join(args[:2])} failed in the train tree: {(done.stderr or done.stdout).strip()[-300:]}")


def _take_theirs(tree: str, path: str) -> None:
    if _git(tree, "cat-file", "-e", f"MERGE_HEAD:{path}").returncode == 0:
        _must(tree, "checkout", "--theirs", "--", path)
        _must(tree, "add", "--", path)
    else:
        _must(tree, "rm", "-q", "--", path)


def _merge(tree: str, n: int, sha: str, generators: dict, into: Assembly) -> None:
    if _git(tree, "merge", "--no-edit", sha).returncode == 0:
        into.merged.append(n)
        return
    settlement = trainpr.settle(_conflicted(tree), generators)
    if settlement.by_hand:
        _must(tree, "merge", "--abort")
        into.left_out.append((n, list(settlement.by_hand)))
        return
    for path in settlement.take:
        _take_theirs(tree, path)
    _must(tree, "commit", "-q", "--no-edit")
    into.merged.append(n)
    into.resolutions += trainpr.resolution_lines(n, settlement)
    into.regenerate += [p for p in settlement.regenerate if p not in into.regenerate]


def _regenerate(tree: str, paths: list, generators: dict) -> None:
    for path in paths:
        done = subprocess.run(generators[path], shell=True, cwd=tree, capture_output=True, text=True)
        if done.returncode != 0:
            raise world.Failed(f"the generator for {path} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    if paths:
        _must(tree, "add", "-A", "--", *paths)
        if _git(tree, "diff", "--cached", "--quiet").returncode != 0:
            _must(tree, "commit", "-q", "-m", "Generated files match the merged tree")


def assemble(name: str, numbers: list, heads: dict, generators: dict) -> Assembly:
    """Merges `numbers` in order onto main in a worktree called `name`, resolving what may be and skipping the rest."""
    tree = os.path.join(config.WORK, name)
    discard(tree)
    world.git("worktree", "add", "-q", "--detach", tree, world.MAIN)
    made = Assembly(tree)
    for n in numbers:
        _merge(tree, n, heads[n], generators, made)
    _regenerate(tree, made.regenerate, generators)
    return made


def discard(tree: str) -> None:
    world.git("worktree", "remove", "--force", tree, check=False)
    shutil.rmtree(tree, ignore_errors=True)


def _branch(date: str) -> str:
    existing = [line.split("refs/heads/", 1)[1] for line in world.git("ls-remote", "--heads", "origin", f"train-{date}-*").splitlines()]
    return trainpr.next_branch(date, existing)


def _queue(items: list) -> None:
    with state.change() as s:
        keys = {i["key"] for i in items}
        s["todo"] = [t for t in s["todo"] if t["key"] not in keys] + items


def _last_ci_change() -> str:
    """The newest commit on main that touched the workflows or the check scripts, or ''."""
    return world.git("log", "-1", "--format=%H", world.MAIN, "--", ".github/workflows", "scripts/checks.sh",
                     "scripts/areas", "tools/nzfleet/nzfleet/selection").strip()


def run(numbers: list, generators: dict, limit: int = 0, say=print) -> int:
    """Puts `numbers` (all eligible open PRs when empty) into one pull request. Returns the exit code."""
    s = state.read()
    open_prs = world.open_prs()
    wanted = numbers or trainpr.eligible(open_prs, s["agents"], s["holds"], s["batches"])
    if not wanted:
        say("nothing to land")
        return 0
    world.fetch(wanted)
    heads = {n: world.head_of(n) for n in wanted}
    files = {n: tuple(world.changed_files(heads[n])) for n in wanted}
    ordered = trainpr.order(trainpr.solo_ci([trainpr.Candidate(n, files[n]) for n in wanted]))
    ordered = ordered[:limit] if limit else ordered
    if config.JEV_SHADOW or config.JEV_ACT:
        _shadow_membership(ordered, heads)
    stamp = datetime.date.today().isoformat()
    probe = f"nzf-train-{stamp}-{os.getpid()}-probe"
    try:
        first = assemble(probe, ordered, heads, generators)
        left_out = first.left_out
        compiled, dropped = trainpr.drop_breakers(
            first.merged, lambda prefix: trainbuild.compile_error(assemble(probe, prefix, heads, generators).tree))
        if not compiled:
            say("nothing to land: " + ("; ".join(f"#{n} does not compile" for n, _ in dropped) or "every change conflicts"))
            return 0
        final = assemble(probe, compiled, heads, generators)
        left_out = _left_out(first.left_out, compiled, final)
        with state.change() as st:
            st["train_refused"] = {}
        return _open(final, left_out, dropped, open_prs, [], say, heads, any(plan.touches_ci(files[n]) for n in compiled))
    finally:
        discard(os.path.join(config.WORK, probe))


def _shadow_membership(ordered: list[int], heads: dict[int, str]) -> None:
    """Logs Jev's next-train choice beside the ordering rule without changing membership."""
    members = {f"ADD_{number}": f"add:{number}" for number in ordered}
    members.update(CLOSE_TRAIN="close_train", STOP="stop")
    choice = enum.Enum("TrainMembership", members)
    point = jev.Point("train.membership", choice, jev.Consequence.REVERSIBLE,
                      frozenset({"candidates", "current_train"}))
    files = {number: world.changed_files(heads[number]) for number in ordered}
    state = {"candidates": [{"pr": number, "file_count": len(files[number]), "areas": sorted(world.areas(files[number], "train"))}
                             for number in ordered], "current_train": []}
    options = [(choice[f"ADD_{number}"], f"Add PR #{number}") for number in ordered]
    options.append((choice.CLOSE_TRAIN, "Close the train"))
    rule = jev.Rule(lambda _state, _values: f"add:{ordered[0]}" if ordered else "close_train")
    try:
        jev.decide(point, jev.Shadow(rule, jev.TypeSafe()), state, options,
                   "Choose the next eligible pull request for this train.", jev.Log(), datetime.datetime.now(datetime.timezone.utc))
    except (jev.JevError, OSError, ValueError):
        return


def _left_out(initial: list, compiled: list, final: Assembly) -> list:
    """Keeps members the final assembly could not merge, including ones absent from its merged list."""
    reasons = {number: files for number, files in [*initial, *final.left_out]}
    for number in compiled:
        if number not in final.merged:
            reasons.setdefault(number, [])
    return list(reasons.items())


def _open(final: Assembly, left_out: list, dropped: list, open_prs: list, screens: list, say, heads: dict, ci: bool = False) -> int:
    members = final.merged
    branch = _branch(datetime.date.today().isoformat())
    sha = world.git("rev-parse", "HEAD", cwd=final.tree).strip()
    world.git("push", "-q", "--no-verify", "origin", f"{sha}:refs/heads/{branch}", cwd=final.tree)
    reasons = [(n, trainpr.left_out_reason(files)) for n, files in left_out]
    broke = [(n, error.strip().splitlines()[0] if error.strip() else "the build failed") for n, error in dropped]
    pr = world.create_pr(branch, trainpr.title(len(members)),
                         trainpr.body(members, final.resolutions, reasons, broke, ["screenshots were drawn on this machine's fonts", *([trainpr.CI_NOTICE] if ci else [])], screens))
    _queue([{"key": f"rebase-{n}", "task": trainpr.rebase_brief(n, pr, files), "model": "sonnet", "area": "", "after": [f"pr:{pr}"],
             "kind": "rebase", "pr": n}
            for n, files in left_out]
           + [{"key": f"fix-compile-{n}", "task": trainpr.fix_brief(n, pr, error), "model": "sonnet", "area": "", "after": [],
              "kind": "fix-compile", "pr": n}
              for n, error in dropped])
    with state.change() as st:
        st["train_prs"][str(pr)] = {"branch": branch, "head": sha,
                                    "source_heads": {str(n): heads[n] for n in members},
                                    "members": members, "left_out": [n for n, _ in left_out],
                                    "dropped": [n for n, _ in dropped], "compiled": True, "ci": ci,
                                    "reviewed": "", "mailing": False, "mailed": False}
    proof_areas = trainpr.ci_proof_areas(_last_ci_change(), state.read()["main_ci"], world.is_ancestor)
    batch.start_batch([pr], areas=sorted(batch.areas_of([sha]) | proof_areas) if proof_areas else None)
    say(f"#{pr}: {trainpr.title(len(members))} ({branch})")
    return 0


def _waiting(pr: str, record: dict, s: dict) -> str:
    world.fetch([int(pr)])
    head = world.head_of(int(pr))
    passed = s["passed"].get(pr)
    files = world.changed_files(head)
    required = trainpr.required_areas(files, world.areas(files, "train"), config.SKIP_AREAS)
    required |= trainpr.ci_proof_areas(_last_ci_change(), s["main_ci"], world.is_ancestor)
    return trainpr.waiting_for(record, passed, s["batches"].get(passed["batch"], {}) if passed else {}, head,
                               world.verified_tree([(int(pr), head)]), required)


def mail_released_trains(say=print) -> None:
    """Mails the owner the one merge command for each train whose exact tree passed every required check and
    whose head a reviewer released, and only then takes it out of draft."""
    s = state.read()
    for pr, record in sorted(s["train_prs"].items()):
        if record.get("mailed") is not False or record.get("mailing") or not record.get("reviewed"):
            continue
        reason = _waiting(pr, record, s)
        if reason:
            say(f"train #{pr} not mailed: {reason}")
            continue
        with state.change() as st:
            current = st["train_prs"].get(pr, {})
            if current.get("mailed") is not False or current.get("mailing") or current.get("reviewed") != record["reviewed"]:
                continue
            current["mailing"] = True
        latest = state.read()
        record = latest["train_prs"][pr]
        reason = _waiting(pr, record, latest)
        if reason:
            with state.change() as st:
                st["train_prs"][pr]["mailing"] = False
            say(f"train #{pr} not mailed: {reason}")
            continue
        world.mark_ready(int(pr))
        subject, text = trainpr.letter(int(pr), record["members"], [(n, "") for n in record["left_out"]],
                                       [(n, "") for n in record["dropped"]], config.UPSTREAM, record["reviewed"], record.get("ci", False))
        if not mail.send(subject, text):
            with state.change() as st:
                st["train_prs"][pr]["mailing"] = False
            say(f"train #{pr} mail did not send")
            continue
        with state.change() as st:
            st["train_prs"][pr]["mailed"] = True
            st["train_prs"][pr]["mailing"] = False


def release(pr: int) -> int:
    """Records that the train's current head has been reviewed, then mails it if the checks are also complete."""
    world.fetch([pr])
    with state.change() as s:
        if str(pr) not in s["train_prs"]:
            print(f"#{pr} is not a train pull request")
            return 1
        s["train_prs"][str(pr)]["reviewed"] = world.head_of(pr)
    mail_released_trains()
    return 0


def queue_red_fixes() -> None:
    """Queues the fix tasks for each train whose merge check failed, once."""
    s = state.read()
    for pr, record in sorted(s["train_prs"].items()):
        failure = s["failed"].get(pr)
        if failure and failure["sha"] != record.get("fixed_for"):
            _queue(trainpr.red_fix_tasks(int(pr), failure))
            with state.change() as st:
                st["train_prs"][pr]["fixed_for"] = failure["sha"]
