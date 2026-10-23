"""The tidy pass's hands and eyes: reads the machine into a tidy.Snapshot and carries out what tidy.decide returns."""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field

from . import config, disktriage, jev, procs, tidy

DATABASE_PORT = re.compile(r"@[^/:]+:(\d+)/")
PUBLISHED_PORT = re.compile(r":(\d+)->5432/tcp")
CLIENTS = ("select count(*) from pg_stat_activity where backend_type = 'client backend' and pid <> pg_backend_pid()")


def run_tool(argv: list) -> tuple:
    done = subprocess.run(argv, capture_output=True, text=True)
    return done.returncode, done.stdout


def run_tool_bytes(argv: list) -> tuple:
    done = subprocess.run(argv, capture_output=True)
    return done.returncode, done.stdout


@dataclass
class Host:
    """The places the pass looks in, and the tools it uses. The defaults are the real machine."""
    builds: list                  # directories whose children are build outputs
    work: str                     # directories of agent worktrees
    repo: str                     # the checkout that owns those worktrees
    unpacks: str = "/tmp"
    proc: str = "/proc"
    run: object = run_tool
    run_bytes: object = run_tool_bytes
    remove_tree: object = shutil.rmtree
    now: object = time.time
    state: dict = field(default_factory=dict)
    caches: tuple = ()            # (directory, cap in GB, lock file or ""): a lock another process holds means the directory is in use
    jev_act: bool = False         # production callers opt into the logged Jev cache choice


def _latest(path: str, depth: int, skip: str) -> float:
    try:
        latest = os.lstat(path).st_mtime
        if depth == 0:
            return latest
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
                elif not (skip and entry.name.startswith(skip)):
                    latest = max(latest, _latest(entry.path, depth - 1, skip))
    except OSError:
        pass
    return latest


def newest(path: str, now: float, depth: int, skip: str = "") -> float:
    """Seconds since the most recent change in `path`, looking `depth` levels down and ignoring subdirectories that start with `skip`."""
    return max(0.0, now - _latest(path, depth, skip))


def _children(base: str) -> list:
    try:
        return sorted(e.path for e in os.scandir(base) if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def live_processes(proc: str) -> tuple:
    """Paths and arguments held by running processes, and their database ports."""
    paths, args, ports = set(), set(), set()
    for entry in _children(proc):
        if not os.path.basename(entry).isdigit():
            continue
        try:
            paths.add(os.path.realpath(os.readlink(os.path.join(entry, "cwd"))))
        except OSError:
            pass
        try:
            with open(os.path.join(entry, "environ"), "rb") as f:
                env = dict(item.partition(b"=")[::2] for item in f.read().split(b"\0") if item)
        except OSError:
            env = {}
        if env.get(b"CARGO_TARGET_DIR"):
            paths.add(env[b"CARGO_TARGET_DIR"].decode(errors="replace"))
        found = DATABASE_PORT.search(env.get(b"DATABASE_URL", b"").decode(errors="replace"))
        if found:
            ports.add(int(found.group(1)))
        try:
            with os.scandir(os.path.join(entry, "fd")) as descriptors:
                fds = [descriptor.path for descriptor in descriptors]
        except OSError:
            fds = []
        for fd in fds:
            try:
                target = os.readlink(fd).removesuffix(" (deleted)")
                if target.startswith("/"):
                    paths.add(os.path.realpath(target))
            except OSError:
                pass
        try:
            with open(os.path.join(entry, "cmdline"), "rb") as f:
                args.update(part.decode(errors="replace") for part in f.read().split(b"\0") if part)
        except OSError:
            pass
    return frozenset(paths), tuple(args), frozenset(ports)


def _containers(host: Host) -> tuple:
    rc, out = host.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    found = []
    for line in out.splitlines() if rc == 0 else []:
        name, _, ports = line.partition("\t")
        if not name.startswith(tidy.CONTAINER_PREFIXES):
            continue
        rc, started = host.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", name])
        if rc != 0:
            continue
        rc, count = host.run(["docker", "exec", name, "psql", "-U", "postgres", "-tAc", CLIENTS])
        if rc != 0 or not count.strip().isdigit():
            continue
        rc, states = host.run(["docker", "top", name, "-eo", "stat"])
        zombies = sum(1 for s in states.splitlines()[1:] if s.startswith("Z")) if rc == 0 else 0
        began = datetime.datetime.fromisoformat(started.strip()[:19] + "+00:00").timestamp()
        port = PUBLISHED_PORT.search(ports)
        found.append(tidy.Container(name, host.now() - began, int(count), int(port.group(1)) if port else None, zombies))
    return tuple(found)


def _networks(host: Host) -> tuple:
    rc, out = host.run(["docker", "network", "ls", "--format", "{{.Name}}"])
    found = []
    for name in sorted(n for n in out.split() if n.startswith(tidy.BATCH_PREFIX)) if rc == 0 else []:
        rc, attached = host.run(["docker", "network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", name])
        found.append(tidy.Network(name, tuple(attached.split()) if rc == 0 else ()))
    return tuple(found)


def _checkouts(host: Host) -> tuple:
    found = []
    for path in _children(host.work):
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        idle = newest(path, host.now(), 2, skip="target")
        if idle < tidy.CHECKOUT_IDLE:
            continue  # the git questions below cost a process each
        rc, status = host.run(["git", "-C", path, "status", "--porcelain", "--untracked-files=all"])
        dirty = rc != 0 or bool(status.strip())
        rc, remotes = host.run(["git", "-C", path, "branch", "-r", "--contains", "HEAD"])
        unpublished = rc != 0 or not remotes.strip()
        found.append(tidy.Checkout(path, idle, dirty, unpublished))
    return tuple(found)


def _rescue_checkout(host: Host, path: str) -> tuple:
    """Saves a worktree's commits and uncommitted files outside the tree before removal."""
    rc, head = host.run(["git", "-C", path, "rev-parse", "HEAD"])
    if rc != 0 or not head.strip():
        return f"could not identify HEAD for {path}", ""
    key = hashlib.sha256(path.encode()).hexdigest()[:12]
    target = os.path.join(host.work, ".rescued", f"{os.path.basename(path)}-{key}-{head.strip()}")
    if os.path.isfile(os.path.join(target, "complete")):
        return "", target
    if os.path.isdir(target):
        try:
            shutil.rmtree(target)
        except OSError as error:
            return f"could not remove incomplete rescue for {path}: {error}", ""
    try:
        os.makedirs(target, exist_ok=False)
    except OSError as error:
        return f"could not create rescue for {path}: {error}", ""
    rc, branch = host.run(["git", "-C", path, "symbolic-ref", "--quiet", "HEAD"])
    current_branch = branch.strip() if rc == 0 else ""
    head_ref = current_branch or "HEAD"
    rc, refs = host.run(["git", "-C", path, "for-each-ref", "--format=%(refname)"])
    if rc != 0:
        return f"could not identify refs for {path}", ""
    other_refs = [ref for ref in refs.splitlines() if ref != current_branch]
    unique_commits = ["git", "-C", path, "rev-list", "--count", head_ref, "--not", *other_refs]
    rc, count = host.run(unique_commits)
    if rc != 0:
        return f"could not identify unreferenced commits from {path}", ""
    if count.strip() != "0":
        bundle = ["git", "-C", path, "bundle", "create", os.path.join(target, "history.bundle"),
                  head_ref, "--not", *other_refs]
        rc, _ = host.run(bundle)
        if rc != 0:
            return f"could not bundle commits from {path}", ""
    rc, patch = host.run_bytes(["git", "-C", path, "diff", "--binary", "HEAD"])
    if rc != 0:
        return f"could not save tracked changes from {path}", ""
    try:
        with open(os.path.join(target, "working-tree.patch"), "wb") as saved:
            saved.write(patch)
    except OSError as error:
        return f"could not save tracked changes from {path}: {error}", ""
    rc, names = host.run(["git", "-C", path, "ls-files", "--others", "--exclude-standard", "-z"])
    if rc != 0:
        return f"could not list untracked files from {path}", ""
    try:
        with tarfile.open(os.path.join(target, "untracked.tar"), "w") as archive:
            for name in names.split("\0"):
                if name:
                    archive.add(os.path.join(path, name), arcname=name, recursive=True)
    except OSError as error:
        return f"could not save untracked files from {path}: {error}", ""
    try:
        with open(os.path.join(target, "head"), "w") as saved:
            saved.write(head.strip() + "\n")
        with open(os.path.join(target, "complete"), "w") as saved:
            saved.write("complete\n")
    except OSError as error:
        return f"could not mark rescue complete for {path}: {error}", ""
    return "", target


def _bytes(path: str) -> int:
    total = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def _gb(path: str) -> int:
    return (_bytes(path) + 2**30 - 1) // 2**30


def _locked(lock: str) -> bool:
    if not lock or not os.path.exists(lock):
        return False
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False


def _caches(host: Host) -> tuple:
    return tuple(tidy.Sized(path, _gb(path), cap, _locked(lock)) for path, cap, lock in host.caches if os.path.isdir(path))


def observe(host: Host) -> tidy.Snapshot:
    now = host.now()
    held_paths, held_args, held_ports = live_processes(host.proc)
    # A batch is recorded with pid 0 until its process has started; that is a live batch, not a dead one.
    batches = frozenset(b for b, rec in host.state.get("batches", {}).items()
                        if rec.get("status") == "running" and (rec.get("pid", 0) == 0 or procs.alive(rec["pid"])))
    unpacks = [p for p in _children(host.unpacks) if os.path.basename(p).startswith("robolectric")]
    return tidy.Snapshot(
        builds=tuple(tidy.Tree(p, newest(p, now, 3)) for base in host.builds for p in _children(base)),
        unpacks=tuple(tidy.Tree(p, newest(p, now, 0)) for p in unpacks),
        containers=_containers(host), networks=_networks(host), checkouts=_checkouts(host),
        held_paths=held_paths, held_args=held_args, held_ports=held_ports, batches=batches,
        starts=tuple((slug, w["path"]) for slug, w in host.state.get("worktrees", {}).items()),
        caches=_caches(host),
    )


def _remove_tree(host: Host, path: str) -> str:
    """Removes `path`; what a container ran as root left behind is removed through docker."""
    try:
        host.remove_tree(path)
    except OSError:
        parent = os.path.dirname(path)
        host.run(["docker", "run", "--rm", "--init", "-v", f"{parent}:/b", "alpine", "rm", "-rf", f"/b/{os.path.basename(path)}"])
    return "" if not os.path.lexists(path) else f"{path} is still there after trying to remove it"


def carry_out(host: Host, action) -> str:
    """Does one action. Returns the line to log; a failure is a line, not an exception."""
    if isinstance(action, tidy.RemoveTree):
        if action.path == os.path.join(host.work, ".rescued"):
            problem = _prune_rescues(host, 20 * 2**30)
        else:
            problem = _remove_tree(host, action.path)
    elif isinstance(action, tidy.RemoveContainer):
        rc, out = host.run(["docker", "rm", "-f", action.name])
        problem = "" if rc == 0 else f"docker rm -f {action.name} failed"
    elif isinstance(action, tidy.RemoveNetwork):
        for container in action.containers:
            host.run(["docker", "rm", "-f", container])
        rc, out = host.run(["docker", "network", "rm", action.name])
        problem = "" if rc == 0 else f"docker network rm {action.name} failed"
    else:
        rescue_path = ""
        if action.rescue:
            problem, rescue_path = _rescue_checkout(host, action.path)
            if problem:
                return f"could not tidy {action.path}: {problem}; worktree kept"
        rc, out = host.run(["git", "-C", host.repo, "worktree", "remove", "--force", action.path])
        problem = "" if rc == 0 else f"git worktree remove failed for {action.path}"
    target = getattr(action, "path", None) or action.name
    if problem:
        return f"could not tidy {target}: {problem}"
    saved = f"; rescue saved to {rescue_path}" if isinstance(action, tidy.RemoveCheckout) and rescue_path else ""
    return f"tidied {target}: {action.why}{saved}"


def run(host: Host, dry_run: bool = False) -> list:
    """One tidy pass. Returns the lines to log."""
    snap = observe(host)
    actions = tidy.decide(snap)
    if dry_run:
        return [f"would tidy {getattr(a, 'path', None) or a.name}: {a.why}" for a in actions] + tidy.zombie_lines(snap)
    lines = []
    cleared = ""
    if host.jev_act and config.JEV_ACT:
        log = jev.Log()
        try:
            cleared, decision_id = disktriage.clear_choice(snap.caches, jev.TypeSafe(), log,
                                                            datetime.datetime.now(datetime.timezone.utc),
                                                            [name for name, batch in host.state.get("batches", {}).items()
                                                             if batch.get("status") == "running"],
                                                            sum(agent.get("status") == "running"
                                                                for agent in host.state.get("agents", {}).values()))
        except (jev.JevError, OSError, ValueError):
            cleared, decision_id = "", None
        if cleared and decision_id:
            before = shutil.disk_usage("/srv/fleet").free
            lines.append(carry_out(host, RemoveTree(cleared, "Jev selected this unlocked cache")))
            after = shutil.disk_usage("/srv/fleet").free
            log.set_outcome(decision_id, "ok" if after - before >= 2 * 2**30 else "wrong",
                            "ok" if after - before >= 2 * 2**30 else "wrong")
    actions = [action for action in actions if getattr(action, "path", None) != cleared]
    lines.extend(carry_out(host, action) for action in actions)
    lines.extend(tidy.zombie_lines(snap))
    if any(isinstance(a, tidy.RemoveCheckout) for a in actions):
        host.run(["git", "-C", host.repo, "worktree", "prune"])
    return lines


def _prune_rescues(host: Host, cap: int) -> str:
    root = os.path.join(host.work, ".rescued")
    try:
        rescues = sorted((os.path.getmtime(os.path.join(root, name)), name)
                         for name in os.listdir(root) if os.path.isdir(os.path.join(root, name)))
    except OSError as error:
        return f"could not inspect rescues: {error}"
    sizes = {name: _bytes(os.path.join(root, name)) for _, name in rescues}
    total = sum(sizes.values())
    for _, name in rescues:
        if total <= cap:
            break
        path = os.path.join(root, name)
        try:
            with open(os.path.join(path, "head")) as saved:
                head = saved.read().strip()
        except OSError:
            continue
        rc, refs = host.run(["git", "-C", host.repo, "for-each-ref", "--format=%(refname)", "--contains", head])
        if rc != 0 or not refs.strip():
            continue
        problem = _remove_tree(host, path)
        if problem:
            return problem
        total -= sizes[name]
    return "" if total <= cap else f"rescue directory remains over {cap // 2**30} GB; no referenced rescues can be pruned"


def caches(home: str, warm: str, sccache: str, cache: str, work: str = "") -> tuple:
    """The directories the tidy pass keeps under a size, as `Host.caches` takes them. A slot's build directory is
    held while a batch has the slot's lock."""
    found = [(sccache, 80, ""), (os.path.join(cache, "npm"), 8, ""), (os.path.join(cache, "cargo-registry"), 20, "")]
    if work:
        found.append((os.path.join(work, ".rescued"), 20, ""))
    if warm and os.path.isdir(warm):
        for name in sorted(os.listdir(warm)):
            if name.startswith("slot-") and name.endswith(".lock"):
                slot = os.path.join(warm, name[:-5])
                found.append((os.path.join(slot, "target"), 80, os.path.join(warm, name)))
    return tuple(found)
