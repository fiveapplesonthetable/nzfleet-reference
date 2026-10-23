"""`nzfleet starve -- <cmd>`: runs a command on a slice of the CPUs with burners around it, so the rest of the machine keeps working."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from . import procs, state

SLICE = 4
MAX_CPU_SHARE = 8
MAX_CALL_SECONDS = 5 * 60
BURNERS_PER_CPU = 4
BURNER_LIFE = MAX_CALL_SECONDS
_LIST = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


@dataclass(frozen=True)
class Plan:
    cpus: str
    burners: int
    wrap: list | None
    problem: str = ""


def _count(cpu_set: str) -> int:
    total = 0
    for part in cpu_set.split(","):
        low, _, high = part.partition("-")
        total += int(high or low) - int(low) + 1
    return total


def _format(cpu_ids: list[int]) -> str:
    parts = []
    values = iter(cpu_ids)
    first = next(values, None)
    if first is None:
        return ""
    start = previous = first
    for cpu in values:
        if cpu != previous + 1:
            parts.append(f"{start}-{previous}" if start != previous else str(start))
            start = cpu
        previous = cpu
    parts.append(f"{start}-{previous}" if start != previous else str(start))
    return ",".join(parts)


def _within(cpu_set: str, available: list[int]) -> bool:
    for part in cpu_set.split(","):
        low = int(part.partition("-")[0])
        high = int(part.rpartition("-")[2])
        if sum(low <= cpu <= high for cpu in available) != high - low + 1:
            return False
    return True


def _valid(cpu_set: str) -> bool:
    return bool(_LIST.match(cpu_set)) and all(int(p.partition("-")[0]) <= int(p.rpartition("-")[2]) for p in cpu_set.split(","))


def plan_starve(total_cpus: int, size: int, cpu_set: str | None, taskset: bool, scope: bool,
                available_cpus: list[int] | None = None) -> Plan:
    """The CPU set, burner count and pinning prefix. The default set is the highest CPUs, so the lowest stay with the machine."""
    available = available_cpus if available_cpus is not None else list(range(total_cpus))
    limit = max(1, total_cpus // MAX_CPU_SHARE)
    if size < 1:
        return Plan(cpu_set or "", 0, None, "the CPU slice must contain at least one CPU")
    if cpu_set is None:
        size = min(size, limit)
        cpu_set = _format(available[-size:])
    if not _valid(cpu_set):
        return Plan(cpu_set, 0, None, f"{cpu_set!r} is not a CPU list; write e.g. 0-3,8")
    if not _within(cpu_set, available):
        return Plan(cpu_set, 0, None, f"{cpu_set} names CPUs outside this process's available affinity")
    if _count(cpu_set) > limit:
        return Plan(cpu_set, 0, None, f"{cpu_set} exceeds the starve limit of {limit} CPUs (one eighth of this {total_cpus}-CPU fleet)")
    burners = BURNERS_PER_CPU * _count(cpu_set)
    if taskset:
        return Plan(cpu_set, burners, ["taskset", "-c", cpu_set])
    if scope:
        return Plan(cpu_set, burners, ["systemd-run", "--user", "--scope", "--quiet", "-p", f"AllowedCPUs={cpu_set}"])
    return Plan(cpu_set, burners, None, "neither taskset nor a systemd user scope is available to pin the CPUs; install util-linux")


def _bounded_call(command: list, env: dict, timeout: int) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        procs.kill_group(proc.pid, now=True)
        proc.wait()
        raise
    return subprocess.CompletedProcess(command, proc.returncode)


def run(command: list, plan: Plan, spawn=subprocess.Popen, call=None, stop=procs.kill_group, say=print) -> int:
    """Starts the burners, runs the command pinned beside them, and stops every burner it started. Returns the command's exit code."""
    if plan.wrap is None:
        say(plan.problem)
        return 1
    env = {**os.environ, "NZFLEET_STARVE": "1"}
    call = call or _bounded_call
    say(f"running on CPUs {plan.cpus} with {plan.burners} burners for at most {MAX_CALL_SECONDS} seconds")
    started = []
    key = str(os.getpid())
    try:
        with state.change() as data:
            if any(b["status"] == "running" for b in data["batches"].values()):
                say("refusing to starve while a merge-check batch is running")
                return 1
            data["starves"][key] = {"pids": started}
            for _ in range(plan.burners):
                started.append(spawn([*plan.wrap, "timeout", str(BURNER_LIFE), "yes"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, start_new_session=True, env=env).pid)
        try:
            return call([*plan.wrap, *command], env=env, timeout=MAX_CALL_SECONDS).returncode
        except subprocess.TimeoutExpired:
            say(f"command exceeded the {MAX_CALL_SECONDS}-second starve limit")
            return 124
        except OSError as e:
            say(f"could not run {command[0]}: {e}")
            return 127
    finally:
        for pid in started:
            stop(pid)
        with state.change() as data:
            data["starves"].pop(key, None)


def stop_for_batch(data: dict, stop=None) -> None:
    """Stops burner groups registered by starve calls when a batch claims the state lock."""
    stop = stop or stop_burner
    for run in data["starves"].values():
        for pid in run["pids"]:
            stop(pid)


def stop_burner(pid: int) -> None:
    """Stops a registered burner only while its process group still belongs to this starvation run."""
    try:
        if os.getpgid(pid) != pid:
            return
        with open(f"/proc/{pid}/environ", "rb") as source:
            environment = source.read().split(b"\0")
        with open(f"/proc/{pid}/cmdline", "rb") as source:
            command = source.read().split(b"\0")
    except OSError:
        return
    if b"NZFLEET_STARVE=1" in environment and b"yes" in command:
        procs.kill_group(pid)
