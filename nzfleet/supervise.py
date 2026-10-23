"""What the hourly supervision decides: which agent to resume or nudge, which of our own processes to stop, why a chain is idle."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from . import plan, runs
from .progress import Verdict

BURNERS = frozenset({"yes", "stress", "stress-ng"})
BURNER_LIMIT = 10 * 60
_RUN_SCOPE = re.compile(r"^run-u\d+\.scope$")


class Step(Enum):
    NONE = "none"
    RESUME = "resume"
    NUDGE = "nudge"
    EMAIL = "email"


@dataclass(frozen=True)
class Proc:
    """One process: its parentage, how long it has run and the CPU seconds it and its reaped children used."""
    pid: int
    ppid: int
    pgid: int
    name: str
    age: float
    cpu: float
    cwd: str
    starved: bool  # started under `nzfleet starve`, which has its own time box
    argv: tuple = ()
    cgroup: str = ""


@dataclass(frozen=True)
class Idle:
    """A chain with nothing running: its head, why it is not starting, and the task whose `PR: none` may be the false block."""
    area: str
    key: str
    why: str
    no_pr_of: str = ""


def below(procs: list, root: int) -> list:
    """`root` and everything descended from it."""
    kids: dict = {}
    for p in procs:
        kids.setdefault(p.ppid, []).append(p)
    out, queue = [], [p for p in procs if p.pid == root]
    while queue:
        p = queue.pop()
        out.append(p)
        queue.extend(kids.get(p.pid, []))
    return out


def tree_cpu(procs: list, root: int) -> float:
    return sum(p.cpu for p in below(procs, root))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def in_agent_cgroup(cgroup: str) -> bool:
    """Under the agent slice, or in one of the user's `run-u<N>.scope` units, which is where an agent's processes live."""
    return any(part.startswith("agent") and part.endswith(".slice") or _RUN_SCOPE.match(part) for part in cgroup.split("/"))


def _command(argv: tuple) -> str:
    """The program a `timeout` invocation runs: the first word after its options and its duration."""
    rest = [a for a in argv[1:] if not a.startswith("-")]
    return os.path.basename(rest[1]) if len(rest) > 1 else ""


def is_burner(p: Proc) -> bool:
    return p.name in BURNERS or (p.name == "timeout" and _command(p.argv) in BURNERS)


def burners(procs: list, agent_roots: set, work: str) -> list:
    """CPU burners left running past the time box: by cgroup, by descent from an agent we started, or by working in a worktree."""
    ours = {p.pid for root in agent_roots for p in below(procs, root)}
    return [p for p in procs if is_burner(p) and p.age >= BURNER_LIMIT and not p.starved
            and (p.pid in ours or _inside(p.cwd, work) or in_agent_cgroup(p.cgroup))]


def owner(burner: Proc, procs: list, agents: dict) -> str:
    """The key of the running agent whose cgroup holds the burner, else the one it descends from, else empty."""
    running = {k: a for k, a in agents.items() if a["status"] == "running"}
    for key, a in running.items():
        mine = next((p for p in procs if p.pid == a["pid"]), None)
        if mine and burner.cgroup and mine.cgroup == burner.cgroup:
            return key
    for key, a in running.items():
        if any(p.pid == burner.pid for p in below(procs, a["pid"])):
            return key
    return ""


def duplicate_batches(batches: dict, alive: set) -> list:
    """The newer of any two running batches over the same PRs, as (id, pid): the older has the progress."""
    groups: dict = {}
    for bid, b in batches.items():
        if b["status"] == "running" and b["pid"] in alive:
            groups.setdefault(frozenset(n for n, _ in b["members"]), []).append((b["started"], bid, b["pid"]))
    return [(bid, pid) for group in groups.values() for _, bid, pid in sorted(group)[1:]]


def step(verdict: Verdict, nudged_before: bool) -> Step:
    """What to do about an agent: resume a hung one, nudge a stalled one once, then tell the owner."""
    if verdict is Verdict.HUNG:
        return Step.RESUME
    if verdict is Verdict.STALLED:
        return Step.EMAIL if nudged_before else Step.NUDGE
    return Step.NONE


def idle_chains(todo: list, agents: dict, standing: dict, holds: dict, running: int, slots: int, busy: bool, area_heads: dict) -> list:
    """Chains whose head could be starting but is not: not held, nothing of the chain running, and the machine has room."""
    if busy or running >= slots:
        return []
    out = []
    for area in dict.fromkeys(t["area"] for t in todo if t.get("area")):
        items = [t for t in todo if t.get("area") == area]
        if any(runs.run_state(t["key"], agents) in ("running", "resuming") for t in items):
            continue
        head = next((t for t in items if runs.run_state(t["key"], agents) == "queued"), None)
        if head is None or head["key"] in holds:
            continue
        gate = plan.area_gate(head, todo, agents, standing, area_heads)
        if gate["go"]:
            out.append(Idle(area, head["key"], f"{head['key']} could start and has not; this is a fault in nzfleet"))
            continue
        earlier = [t["key"] for t in items[: items.index(head)]]
        blocked = next((k for k in earlier[-1:] if "stated `PR: none`" in gate["why"]), "")
        out.append(Idle(area, head["key"], gate["why"], blocked))
    return out
