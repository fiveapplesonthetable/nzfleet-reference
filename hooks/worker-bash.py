#!/usr/bin/env python3
# Refuses the Bash calls a headless worker session must not make: a background command, and `git stash`.
import json
import shlex
import sys

BACKGROUND = ("Denied: this session exits when you write your final message, and a background command dies with it. "
              "Run it in the foreground with a timeout, or start it with `nzfleet run` and wait on it with `nzfleet wait`.")
STASH = ("Denied: `git stash` is one ref shared by every worktree of this repository, so a `git stash pop` can apply another "
         "agent's work to yours. To make a test fail first, save the change and remove it: "
         "`git diff -- <paths> > $TMPDIR/x.patch && git checkout -- <paths>`, run the test, then `git apply $TMPDIR/x.patch`. "
         "Or commit to your branch and use `git revert --no-commit`.")

SEPARATORS = ";&|()`\n"
WRAPPERS = {"env", "command", "exec", "time", "nohup", "nice", "sudo"}
SHELLS = {"sh", "bash", "zsh", "dash"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}


def segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    out: list[list[str]] = [[]]
    for token in lexer:
        if set(token) <= set(SEPARATORS):
            out.append([])
        else:
            out[-1].append(token)
    return [words for words in out if words]


def runs_stash(words: list[str]) -> bool:
    i = 0
    while i < len(words) and (words[i] in WRAPPERS or ("=" in words[i] and not words[i].startswith("-"))):
        i += 1
    rest = words[i:]
    if not rest:
        return False
    program = rest[0].rsplit("/", 1)[-1]
    if program in SHELLS and "-c" in rest[1:]:
        at = rest.index("-c") + 1
        return at < len(rest) and stashes(rest[at])
    if program != "git":
        return False
    j = 1
    while j < len(rest) and rest[j].startswith("-"):
        j += 2 if rest[j] in GIT_OPTIONS_WITH_VALUE else 1
    return j < len(rest) and rest[j] == "stash"


def stashes(command: str) -> bool:
    try:
        return any(runs_stash(words) for words in segments(command))
    except ValueError:
        return False


def verdict(tool_input: dict) -> str:
    if tool_input.get("run_in_background"):
        return BACKGROUND
    if stashes(str(tool_input.get("command") or "")):
        return STASH
    return ""


if __name__ == "__main__":
    why = verdict(json.load(sys.stdin).get("tool_input") or {})
    if why:
        print(why, file=sys.stderr)
        sys.exit(2)
