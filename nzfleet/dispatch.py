"""Whether a task may be dispatched at all, and how a run is launched or resumed, as functions of plain data."""

from __future__ import annotations

import json
import os
import shlex
import uuid
from dataclasses import dataclass

from . import runs

ESCALATION_LINES = 20


@dataclass(frozen=True)
class PullFacts:
    """What GitHub and git say about the PR a task names."""
    state: str
    head: str
    branch: str
    branch_exists: bool
    head_exists: bool


def refusal(pr: int, facts: PullFacts) -> str:
    """Why a task for this PR must not start, or an empty string when it may."""
    if facts.state != "OPEN":
        return f"#{pr} is {facts.state.lower()}"
    if not facts.branch_exists:
        return f"the branch {facts.branch} of #{pr} no longer exists"
    if not facts.head_exists:
        return f"the head {facts.head[:8]} of #{pr} no longer exists"
    return ""


def already_ran(key: str, head: str, agents: dict) -> bool:
    """True when a run of this task already started at this head: the same work is never dispatched twice."""
    return bool(head) and any(a.get("head") == head for _, a in runs.lineage(runs.base_key(key), agents))


def new_session() -> str:
    return str(uuid.uuid4())


def has_sessions(command: str) -> bool:
    """True when the agent command is Claude, the one tool whose sessions nzfleet names and resumes."""
    return os.path.basename(shlex.split(command)[0]) == "claude"


def with_session(argv: list, session: str, resume: bool) -> list:
    """Adds the flag that names or resumes a Claude session to the command."""
    return [argv[0], "--resume" if resume else "--session-id", session, *argv[1:]] if session else argv


def settings(hook: str) -> str:
    """The Claude settings that deny a background Bash call, as JSON naming the hook script."""
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 -I {shlex.quote(hook)}"}]}]}})


def with_settings(argv: list, path: str) -> list:
    """Adds the settings file to a Claude command; any other agent command is left as it is."""
    return [argv[0], "--settings", path, *argv[1:]] if has_sessions(argv[0]) else argv


def command_argv(command: str, model: str, brief: str) -> list[str]:
    """Expands a provider command without splitting the prompt into separate arguments."""
    return [part.format(model=model, brief=brief) for part in shlex.split(command)]


def provider_name(command: str, selected: str) -> str:
    """Names the executable actually selected when the fleet session uses generic configuration."""
    if selected != "configured":
        return selected
    words = shlex.split(command)
    return os.path.basename(words[0]) if words else "unknown"


def escalation(key: str, runs_made: int, log: str) -> str:
    """The owner-issue comment for a task that used its one retry: the end of its log."""
    tail = "\n".join([line for line in log.splitlines() if line.strip()][-ESCALATION_LINES:]) or "(its log is empty)"
    return (f"`{key}` ended {runs_made} times without its final report and will not be dispatched again.\n\n"
            f"Last {ESCALATION_LINES} lines of its log:\n\n```\n{tail}\n```")
