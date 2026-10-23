"""Observed inputs for the snapshot: a quiet fleet that a test adds one thing to."""

import copy

from nzfleet import snapshot, state, supervise

NOW = 1_800_000_000.0
HOUR = 3600.0
REPO = "/repo"


def proc(pid, name="bash", ppid=1, age=60.0, cpu=0.0, cwd="/", cgroup="", starved=False):
    return supervise.Proc(pid, ppid, pid, name, age, cpu, cwd, starved, cgroup=cgroup)


def observed(**over):
    """Everything `snapshot.snapshot` takes, for a fleet with nothing running."""
    w = {"now": NOW, "day_start": NOW - 5 * HOUR, "state": copy.deepcopy(state.EMPTY), "procs": [], "agent_pids": {}, "activity": {},
         "machine": {"load": [1.0, 1.0, 1.0], "cores": 16, "mem_total_gb": 64, "mem_free_gb": 40, "temp_c": None, "disk": []},
         "cost_records": [], "previous": {}, "quotas": {}, "repo": REPO}
    w.update(over)
    return w


def taken(**over):
    return snapshot.snapshot(observed(**over))


def with_state(**fields):
    s = copy.deepcopy(state.EMPTY)
    s.update(fields)
    return s


def running_agent(key="app-data", pid=900, started=NOW - HOUR):
    return {"status": "running", "pid": pid, "task": "t", "model": "sonnet", "log": "/log", "started": started, "area": "app"}
