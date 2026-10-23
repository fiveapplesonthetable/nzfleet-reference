"""Whose CPU a process is using, from its cgroup and its place in the process tree, as pure functions of the process table."""

from __future__ import annotations

from . import supervise

OWNERS = ("ci_runners", "batches", "agents", "burners", "orchestrator", "other", "unknown")
_CI_WORKSPACES = ("/__w/nzela/nzela", "/home/runner/work/nzela/nzela")
_BUILD_PROCESSES = frozenset({"cargo", "rustc", "java", "gradle", "kotlinc"})


def _ci_container_cgroups(procs: list) -> set:
    """Docker scopes with a process in GitHub's nzela checkout belong to that CI job."""
    groups = set()
    for p in procs:
        scope = p.cgroup.rpartition("/")[2]
        if scope.startswith("docker-") and scope.endswith(".scope") and any(
                p.cwd == root or p.cwd.startswith(root + "/") for root in _CI_WORKSPACES):
            groups.add(p.cgroup)
    return groups


def _unresolved_ci_process(p) -> bool:
    """A build process in a Docker scope without workspace evidence cannot be placed safely."""
    scope = p.cgroup.rpartition("/")[2]
    return scope.startswith("docker-") and scope.endswith(".scope") and p.name in _BUILD_PROCESSES


def classify(procs: list, agent_pids: dict, repo: str) -> dict:
    """pid -> owner. `agent_pids` maps a running agent's key to its process id; `repo` is the orchestrator's working directory."""
    owner: dict = {}
    for key, root in agent_pids.items():
        for p in supervise.below(procs, root):
            owner[p.pid] = ("agents", key)
    orchestrators = {p.pid for p in procs if p.name == "claude" and p.cwd == repo and p.pid not in owner}
    for root in orchestrators:
        for p in supervise.below(procs, root):
            owner.setdefault(p.pid, ("orchestrator", ""))
    ci_containers = _ci_container_cgroups(procs)
    out = {}
    for p in procs:
        if p.name in supervise.BURNERS and not p.starved:
            out[p.pid] = ("burners", "")
        elif p.pid in owner:
            out[p.pid] = owner[p.pid]
        elif "nzela\\x2drunner" in p.cgroup or "nzela-runner" in p.cgroup or p.cgroup in ci_containers:
            out[p.pid] = ("ci_runners", "")
        elif "/nzf-" in p.cgroup or p.cgroup.rpartition("/")[2].startswith("nzf-"):
            out[p.pid] = ("batches", "")
        elif _unresolved_ci_process(p):
            out[p.pid] = ("unknown", "")
        else:
            out[p.pid] = ("other", "")
    return out


def rates(procs: list, owner_of: dict, previous: dict, elapsed: float) -> tuple:
    """(cores in use by owner, cores by agent key, the CPU seconds each busy process had used).

    A process not seen last time counts over its whole life.
    """
    used, by_agent = {o: 0.0 for o in OWNERS}, {}
    for p in procs:
        before = previous.get(str(p.pid))
        rate = max(0.0, (p.cpu - before) / elapsed if before is not None and elapsed > 0 else p.cpu / max(p.age, 1.0))
        name, key = owner_of[p.pid]
        used[name] += rate
        if key:
            by_agent[key] = by_agent.get(key, 0.0) + rate
    return ({o: round(v, 1) for o, v in used.items()}, {k: round(v, 1) for k, v in by_agent.items()},
            {str(p.pid): p.cpu for p in procs if p.cpu >= 1.0})
