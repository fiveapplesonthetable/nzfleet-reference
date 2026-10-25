"""What the machine's tidy pass removes: pure decisions over a snapshot, returned as actions for janitor.py to carry out."""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import runs, tmpdirs

INTERVAL = 15 * 60
BUILD_IDLE = 90 * 60
CONTAINER_IDLE = 2 * 3600
CHECKOUT_IDLE = 6 * 3600
BATCH_CONTAINER_IDLE = 20 * 60  # a batch's database outlives its batch only when the batch was killed
UNPACK_IDLE = 24 * 3600  # a running test JVM keeps mapping libraries from the directory it unpacked hours earlier
CONTAINER_PREFIXES = ("nz", "agentdb-")
DATABASE_PREFIX = "agentdb-"
BATCH_PREFIX = "nzf-"


@dataclass(frozen=True)
class Tree:
    """A directory and the seconds since anything inside it changed."""
    path: str
    idle: float


@dataclass(frozen=True)
class Sized:
    """A cache directory, its size in GB, the most it may grow to, and whether a build holds it right now."""
    path: str
    gb: int
    cap: int
    held: bool = False


@dataclass(frozen=True)
class Container:
    name: str
    age: float
    clients: int
    port: int | None  # the host port its Postgres answers on
    zombies: int = 0  # processes in it that exited and were never reaped


@dataclass(frozen=True)
class Network:
    """A docker network and the containers attached to it."""
    name: str
    containers: tuple


@dataclass(frozen=True)
class Checkout:
    """An idle worktree candidate."""
    path: str
    idle: float
    dirty: bool = False
    unpublished: bool = False


@dataclass(frozen=True)
class Snapshot:
    builds: tuple = ()
    unpacks: tuple = ()
    containers: tuple = ()
    checkouts: tuple = ()
    networks: tuple = ()                 # the nzf-* networks on the daemon
    held_paths: frozenset = frozenset()  # working directories and CARGO_TARGET_DIRs of live processes
    held_args: tuple = ()                 # command-line arguments of live processes
    held_ports: frozenset = frozenset()  # database ports named in live processes' DATABASE_URL
    batches: frozenset = frozenset()     # ids of batches whose process is alive
    starts: tuple = ()                   # (slug, path) of each worktree `nzfleet start` made
    temps: tuple = ()                    # Tree: our own temporary files, in the system directory and under the fleet's
    scratch: frozenset = frozenset()     # temporary directories named by TMPDIR in live processes
    caches: tuple = ()                   # Sized: the caches batches share and the build directories of warm slots


@dataclass(frozen=True)
class RemoveTree:
    path: str
    why: str


@dataclass(frozen=True)
class RemoveContainer:
    name: str
    why: str


@dataclass(frozen=True)
class RemoveNetwork:
    name: str
    why: str
    containers: tuple  # attached ones, which docker will not let the network go without


@dataclass(frozen=True)
class RemoveCheckout:
    path: str
    why: str
    rescue: bool = False


def due(last: float, now: float) -> bool:
    return now - last >= INTERVAL


def stale_runs(agents: dict, now: float, days: int, live: set, open_prs: set) -> list:
    """Unfinished runs old enough to retire, excluding live processes and runs with an open PR."""
    cutoff = now - days * 24 * 3600
    open_refs = {str(ref) for ref in open_prs}
    return sorted(key for key, run in agents.items()
                  if run.get("status") in ("unfinished", runs.ABANDONED) and run.get("finished", run.get("started", now)) <= cutoff
                  and key not in live and str((run.get("pr") or {}).get("number", "")) not in open_refs
                  and (not run.get("branch") or str(run["branch"]) not in open_refs))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _hours(seconds: float) -> str:
    return f"{int(seconds // 3600)} h" if seconds >= 3600 else f"{int(seconds // 60)} min"


class _Ownership:
    """Whether a name or path belongs to something that is running right now."""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.slugs = dict(snap.starts)

    def used(self, path: str) -> bool:
        return (any(_inside(held, path) for held in self.snap.held_paths)
                or any(path in arg for arg in self.snap.held_args))

    def scratched(self, path: str) -> bool:
        """Whether a live process was given a temporary directory that holds `path`."""
        return any(_inside(path, root) for root in self.snap.scratch)

    def batch(self, name: str) -> bool:
        return any(name.startswith(f"nzf-{batch}") for batch in self.snap.batches)

    def slug(self, name: str) -> bool:
        """A worktree made by `nzfleet start` with a process in it."""
        path = self.slugs.get(name)
        return path is not None and self.used(path)

    def busy(self, name: str, path: str = "") -> bool:
        return self.batch(name) or self.slug(name) or bool(path) and self.used(path)


def zombie_lines(snap: Snapshot) -> list:
    """One line per container that holds unreaped processes."""
    return [f"{c.name} holds {c.zombies} zombie process{'' if c.zombies == 1 else 'es'}" for c in snap.containers if c.zombies]


def decide(snap: Snapshot) -> list:
    """Everything idle, finished or abandoned that no live agent, batch or process is using."""
    own = _Ownership(snap)
    actions = []
    for tree in snap.builds:
        if tree.idle >= BUILD_IDLE and not own.busy(os.path.basename(tree.path), tree.path):
            actions.append(RemoveTree(tree.path, f"build output untouched for {_hours(tree.idle)}"))
    for tree in snap.temps:
        if tree.idle >= tmpdirs.IDLE and not own.used(tree.path) and not own.scratched(tree.path):
            actions.append(RemoveTree(tree.path, f"temporary files untouched for {_hours(tree.idle)}"))
    for c in snap.caches:
        if c.gb > c.cap and not c.held:
            actions.append(RemoveTree(c.path, f"cache is {c.gb} GB, over its {c.cap} GB"))
    for tree in snap.unpacks:
        if tree.idle >= UNPACK_IDLE and not own.used(tree.path):
            actions.append(RemoveTree(tree.path, f"test runtime unpacked {_hours(tree.idle)} ago"))
    for c in snap.containers:
        if c.name.startswith(BATCH_PREFIX) and c.age >= BATCH_CONTAINER_IDLE and not own.batch(c.name):
            actions.append(RemoveContainer(c.name, f"database of a batch that is no longer running, up {_hours(c.age)}"))
            continue
        slug = c.name.removeprefix(DATABASE_PREFIX)
        if (c.name.startswith(CONTAINER_PREFIXES) and c.age >= CONTAINER_IDLE and c.clients == 0
                and c.port not in snap.held_ports and not own.busy(c.name) and not own.slug(slug)):
            actions.append(RemoveContainer(c.name, f"up {_hours(c.age)} with no client"))
    for n in snap.networks:
        if n.name.startswith(BATCH_PREFIX) and not own.batch(n.name):
            actions.append(RemoveNetwork(n.name, "network of a batch that is no longer running", n.containers))
    for w in snap.checkouts:
        if w.idle >= CHECKOUT_IDLE and not own.busy(os.path.basename(w.path), w.path):
            rescue = (w.dirty or w.unpublished) and not os.path.basename(w.path).startswith(BATCH_PREFIX)
            actions.append(RemoveCheckout(w.path, f"untouched for {_hours(w.idle)} with no live batch or process", rescue))
    return actions
