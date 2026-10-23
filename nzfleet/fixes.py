"""A PR whose batch is red for a real reason gets one fix agent; if its next head is red too, the owner is told."""

from __future__ import annotations

from . import owner


def key(n) -> str:
    return f"fix-{n}"


def fix_task(n: int, branch: str, failure: dict) -> str:
    """The brief for the agent that repairs PR `n`, whose batch failed with `failure` (a `failed` record)."""
    tests = ", ".join(failure.get("tests") or []) or "none named"
    return (f"The batch for PR #{n} (branch `{branch}`) is red for a reason in its own code; main alone passes the area.\n\n"
            f"Area: {' '.join(failure.get('areas') or []) or 'not recorded'}\nFailed: {failure['why']}\nTests: {tests}\n\nLog excerpt:\n{failure.get('log') or '(none)'}\n\n"
            f"Fix the root cause on the same branch, not by loosening the check. Make a worktree of the branch "
            f"(`git fetch origin {branch}`, then `git worktree add <path> origin/{branch}`), commit the fix there, "
            f"push to `{branch}` without force, and run `nzfleet tick` so the PR is batched again. Do not open a second PR.")


def decide(failure: dict, record, run_state: str) -> str:
    """`start`, `wait`, `tell` or `done` for a PR that has `failure` on its head, given the record of its one fix attempt."""
    if record is None:
        return "start"
    if record["told"]:
        return "done"
    if failure["sha"] != record["sha"]:
        return "tell"
    return "tell" if run_state == "done" else "wait"


def failure_mail(n: int, failure: dict, pushed: bool) -> owner.Mail:
    """What the owner is told when the fix agent's attempt did not turn the batch green."""
    tests = ", ".join(failure.get("tests") or []) or "no test named"
    how = "pushed a fix that is still red" if pushed else "pushed nothing"
    return owner.Mail(f"nzela: #{n} is still red after its fix agent",
                      f"The fix agent for #{n} {how}.\n\nArea: {' '.join(failure.get('areas') or []) or 'not recorded'}\nFailed: {failure['why']}\nTests: {tests}\n\n{failure.get('log') or ''}\n\n"
                      f"It will not get another agent by itself. Push to the branch, or run `nzfleet batch {n}` when the cause is gone.\n")

