"""The PreToolUse rule for agents' Bash commands: a CPU burner is run under `nzfleet starve` or not at all."""

from __future__ import annotations

import re
import shlex

BURNERS = frozenset({"yes", "stress", "stress-ng"})
WRAPPERS = frozenset({"timeout", "sudo", "nohup", "setsid", "env", "nice", "taskset", "xargs", "time", "exec", "command"})
_SEPARATOR = re.compile(r"(\|\||&&|;|\n|\||&|\bdo\b|\bthen\b|\belse\b)")
_STARVE = re.compile(r"\bnzfleet\s+starve\b")
REFUSAL = ("Refused: `{word}` is a CPU burner. Load generation runs only as `nzfleet starve -- <command>`, which gives it a "
           "CPU slice of its own and stops the burners when the command ends. nzfleet kills any other burner after 10 minutes.")


def _word(stage: str) -> str:
    """The program a pipeline stage runs, looking through wrappers, their options and their arguments."""
    try:
        tokens = shlex.split(stage)
    except ValueError:
        tokens = stage.split()
    for token in tokens:
        name = token.lstrip("({").rpartition("/")[2]
        if name in BURNERS:
            return name
        if "=" in token or token.startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", token) or name in WRAPPERS:
            continue
        return ""
    return ""


def refusal(command: str) -> str:
    """The sentence refusing `command`, or empty. `yes` feeding a pipe answers a prompt and is allowed; anywhere else it burns."""
    if _STARVE.search(command):
        return ""
    parts = _SEPARATOR.split(command)
    for i in range(0, len(parts), 2):
        word = _word(parts[i])
        feeds_pipe = i + 1 < len(parts) and parts[i + 1] == "|"
        if word and not (word == "yes" and feeds_pipe):
            return REFUSAL.format(word=word)
    return ""
