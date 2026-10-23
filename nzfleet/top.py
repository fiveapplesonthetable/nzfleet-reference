"""`nzfleet top`: the status sections redrawn in the terminal, newest events at the bottom. `frame` is pure; `run` is the terminal."""

from __future__ import annotations

import json
import os
import select
import sys
import termios
import textwrap
import time
import tty

from . import render, snapshot_tick, tick

EVENT_LINES = 5
REFRESH = 3.0
_SECTION_LINES = (
    ("needs_you", lambda s: render.lines("NEEDS YOU", s["needs_you"], "nothing")),
    ("attention", lambda s: render.lines("NEEDS ATTENTION", s["attention"], "nothing")),
    ("pipeline", lambda s: render.pipeline(s["pipeline"])),
    ("agents", lambda s: render.agents(s["agents"])),
    ("machine", lambda s: render.machine(s["machine"])),
    ("cost", lambda s: render.cost(s["cost"])),
    ("held", lambda s: render.held(s["held"], False)),
)


def _flat(lines: list) -> list:
    return [part for line in lines for part in line.split("\n")]


def _collapsed(lines: list) -> list:
    return [f"{lines[0]} (+{len(lines) - 1} lines)"]


def frame(shot: dict, events: list, rows: int, cols: int) -> list:
    """What to draw on a terminal of `rows` by `cols`: every section in order, the lower ones cut to their title when the screen is short."""
    headline = textwrap.wrap(render.headline(shot["headline"]), cols)[:2]
    sections = [_flat(build(shot)) for _, build in _SECTION_LINES]
    shown_events = events[-EVENT_LINES:] if rows >= 20 else events[-2:]
    room = rows - len(headline) - len(shown_events) - 1
    kept = sections[: max(room, 0)]
    spare = room - len(kept)
    out = []
    for lines in kept:
        extra = len(lines) - 1
        if extra <= spare:
            out += lines
            spare -= extra
        else:
            out += _collapsed(lines)
    body = [line[:cols] for line in headline + out]
    tail = [line[:cols] for line in ["EVENTS"] + shown_events] if shown_events else []
    return body + tail


def _events(path: str) -> list:
    try:
        with open(path) as f:
            return f.read().splitlines()[-EVENT_LINES:]
    except OSError:
        return []


def _read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def draw(shot: dict | None, events: list, rows: int, cols: int, now: float) -> str:
    if shot is None:
        return "no snapshot yet; the daemon writes one every tick, or run: nzfleet tick"
    lines = frame(shot, events, rows - 1, cols)
    footer = f"snapshot {int(now - shot['at'])} s old; q quits"
    return "\n".join(lines[: rows - 1] + [footer])


def run(path: str = snapshot_tick.PATH, events_path: str = tick.EVENTS, refresh: float = REFRESH) -> int:
    """Without a terminal, prints one frame and exits; with one, redraws until q."""
    size = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((100, 40))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(draw(_read(path), _events(events_path), size.lines, size.columns, time.time()))
        return 0
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        while True:
            size = os.get_terminal_size()
            text = draw(_read(path), _events(events_path), size.lines, size.columns, time.time())
            sys.stdout.write("\x1b[H" + "\x1b[K\n".join(text.split("\n")) + "\x1b[K\x1b[J")
            sys.stdout.flush()
            ready, _, _ = select.select([sys.stdin], [], [], refresh)
            if ready and sys.stdin.read(1) in ("q", "Q"):
                return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
