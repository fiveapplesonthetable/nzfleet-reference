"""The `nzfleet/merge-check` commit status: what a batch found, written on the exact head it checked."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from . import config, plan, skips as left_out

CONTEXT = "nzfleet/merge-check"
LIMIT = 140  # GitHub rejects a longer description

# A batch outcome that says nothing about the code is an error, never a pass or a fail.
UNDECIDED = {
    "main-broken": "main fails these checks without the pull request",
    "infrastructure": "the checks could not run on this machine",
    "nothing-merged": "nothing merged, so nothing was checked",
    "counterexample": "a random input failed; a person is reading it",
    "red-once": "the batch failed and is not repeated automatically; a person reads the failing areas",
    "gave-up": "the batch gave up before reaching a verdict",
}


def setup_advice(token_file: str) -> list:
    """What `nzfleet setup` says about the status token; nothing when it is configured."""
    if token_file:
        return []
    return ["NZFLEET_STATUS_TOKEN_FILE is not set, so batches post no `nzfleet/merge-check` commit status; docs/ci-runners.md has the owner's steps."]


def pending() -> tuple:
    return "pending", "merge-check is running"


def verdict(pr: int, outcome: str, passed: list, skipped: list, blamed: dict, areas: list, why: dict | None = None) -> tuple:
    """(state, description) for member `pr` of a finished batch. `blamed` maps a PR to the line that failed it."""
    if pr in passed:
        return "success", "merged onto main, every check passed"
    if pr in blamed:
        named = plan.failed_areas(blamed[pr], areas)
        return "failure", f"checks failed after merging onto main: {' '.join(named)}"
    if pr in skipped:
        return "failure", f"does not merge onto main: {left_out.reason((why or {}).get(str(pr), {}))}"
    return "error", UNDECIDED.get(outcome, "the batch ended before this pull request was decided")


def post_all(members: list, state_of, send, context: str = CONTEXT) -> None:
    """Sends `state_of(pr)` under `context` for each (pr, sha). `send` is None when no token is configured; a failed post is reported, not raised."""
    if send is None:
        return
    for pr, sha in members:
        status, description = state_of(pr)
        try:
            send(sha, status, description[:LIMIT], context)
        except (OSError, ValueError) as e:
            print(f"could not post {context} on #{pr}: {e}", file=sys.stderr)


class GitHub:
    """The one network call: POST /repos/{repo}/statuses/{sha}."""

    def __init__(self, repository: str, token: str):
        self.repository, self.token = repository, token

    @classmethod
    def from_config(cls):
        if not config.STATUS_TOKEN_FILE:
            return None
        try:
            token = open(config.STATUS_TOKEN_FILE).read().strip()
        except OSError as e:
            print(f"cannot read NZFLEET_STATUS_TOKEN_FILE: {e}", file=sys.stderr)
            return None
        return cls(config.UPSTREAM, token) if token else None

    def __call__(self, sha: str, status: str, description: str, context: str = CONTEXT) -> None:
        body = json.dumps({"state": status, "context": context, "description": description}).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/statuses/{sha}", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            urllib.request.urlopen(request, timeout=30).close()
        except urllib.error.HTTPError as e:
            raise OSError(f"GitHub answered {e.code}") from e
