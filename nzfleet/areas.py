"""Runs a batch's areas the way ci runs them: in the job's image with its service, or on the host where ci has no image."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import docker_budget, plan

TUNING = ("CARGO_PROFILE_DEV_DEBUG", "CARGO_PROFILE_TEST_DEBUG", "CARGO_INCREMENTAL")
NOT_STARTED = 97
# An area cut short because another failed; it proves nothing, so it is neither a pass nor a failure.
STOPPED = 98
CGROUPS = "/sys/fs/cgroup/system.slice"
CONTAINER_TARGET = "/target"
# Keep two host CPUs available to VM work outside the batch.
RESERVED_CPUS = 2
SCCACHE_LINE = re.compile(r"^sccache: (\d+) hits, (\d+) misses$", re.M)
# The three ways an area says how many tests passed: cargo, Gradle (via checks.sh) and Playwright.
PASSED = (re.compile(r"^test result: \w+\. (\d+) passed", re.M), re.compile(r"^android tests: (\d+) ran, 0 failed$", re.M),
          re.compile(r"^\s+(\d+) passed \(", re.M))


class CpuMeter:
    """The CPU seconds of cgroups, read while they run because a finished one takes its accounting with it. A
    name is a docker container or, given `scopes`, a systemd unit."""

    def __init__(self, names: list, run=subprocess.run, root: str = CGROUPS, every: float = 2.0, scopes: str = ""):
        self.names, self.run, self.root, self.every, self.scopes = names, run, root, every, scopes
        self.paths: dict = {}
        self.usec: dict = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _path(self, name: str) -> str:
        if self.scopes:
            return os.path.join(self.scopes, f"{name}.scope", "cpu.stat")
        # The container exists only once `docker run` has got as far as creating it.
        found = self.run(["docker", "inspect", "--type", "container", "-f", "{{.Id}}", name], capture_output=True, text=True)
        if found.returncode:
            return ""
        scope = f"docker-{found.stdout.strip()}.scope"
        for parent in (self.root, "/sys/fs/cgroup/user.slice/user-1000.slice"):
            path = os.path.join(parent, scope, "cpu.stat")
            if os.path.exists(path):
                return path
        return ""

    def _poll(self) -> None:
        while True:
            for name in self.names:
                self.paths[name] = self.paths.get(name) or self._path(name)
                if self.paths[name]:
                    self._read(name)
            if self.stop.wait(self.every):
                return

    def _read(self, name: str) -> None:
        try:
            with open(self.paths[name]) as stat:
                for line in stat:
                    if line.startswith("usage_usec "):
                        self.usec[name] = int(line.split()[1])
        except (OSError, ValueError):
            pass  # the cgroup is gone or not made yet; the last reading stands

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()

    @property
    def seconds(self) -> float:
        return sum(self.usec.values()) / 1e6


def user_scopes() -> str:
    """Where systemd keeps this user's transient scopes, or empty where there is no user manager to ask."""
    uid = os.getuid()
    path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice"
    return path if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR") and os.path.isdir(path) else ""


def cache_env(root: str, caches: dict) -> dict:
    """The environment that points a job at the caches mounted into it. A tree that predates the wrapper gets none."""
    env = {"npm_config_prefer_offline": "true"}
    if "/npm" in caches.values():
        env["npm_config_cache"] = "/npm"
    wrapper = os.path.join(root, "scripts", "rustc-wrapper.sh")
    if "/sccache" in caches.values() and os.path.exists(wrapper):
        env.update(RUSTC_WRAPPER=wrapper, SCCACHE_DIR="/sccache", SCCACHE_CACHE_SIZE="60G", SCCACHE_BASEDIRS=root)
    return env


def rust_cache_env(root: str, sccache: str) -> dict:
    """The compiler cache for a job that runs on the host, where there is no mount to point at."""
    wrapper = os.path.join(root, "scripts", "rustc-wrapper.sh")
    return {"RUSTC_WRAPPER": wrapper, "SCCACHE_DIR": sccache, "SCCACHE_CACHE_SIZE": "60G"} if sccache and os.path.exists(wrapper) else {}


def sccache_counts(text: str) -> tuple:
    """Hits and misses of the last `sccache: N hits, M misses` line checks.sh printed, or none."""
    found = SCCACHE_LINE.findall(text)
    return (int(found[-1][0]), int(found[-1][1])) if found else (0, 0)


def tests_passed(text: str) -> int:
    """The tests an area's output says passed. Zero for an area that runs none, or whose output says nothing."""
    return sum(int(n) for pattern in PASSED for n in pattern.findall(text))


def stats_line(area: str, stat: dict) -> str:
    hits, misses = stat["sccache_hits"], stat["sccache_misses"]
    cache = f", sccache {hits} hits {misses} misses" if hits or misses else ""
    tests = f", {stat['tests_passed']} tests passed" if stat["tests_passed"] else ""
    return f"stats: {area} {stat['wall_s']}s wall, {stat['cpu_s'] / 60:.1f} cpu-min{cache}{tests}"


def _healthy(docker, container: str) -> bool:
    """True once the container reports healthy; False once it has stopped, which it will not recover from."""
    while docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip() == "true":
        if docker("inspect", "-f", "{{.State.Health.Status}}", container).stdout.strip() == "healthy":
            return True
        time.sleep(0.5)
    return False


def network_failure(area: str, name: str, stderr: str, networks: int) -> str:
    """The log line for a network docker would not create. Pool exhaustion is named because it is the usual
    cause and the fix is the daemon's, not the pull request's."""
    cause = (f"; the daemon's address pools are exhausted with {networks} networks present, "
             "so tidy the leaked nzf-* ones or widen default-address-pools (nzfleet setup prints how)"
             if "address pool" in stderr else "")
    return f"{area}: docker could not create the network {name}{cause}\n{stderr.strip()}\n"


def _ensure_network(docker, area: str, name: str, out) -> bool:
    """Makes sure `name` exists, and says why in `out` when it cannot. Called again just before the job
    starts, because the service's wait can be long enough for something else on the daemon to have removed an
    unattached network."""
    if docker("network", "inspect", name).returncode == 0:
        return True
    made = docker("network", "create", name)
    if made.returncode == 0:
        return True
    networks = len(docker("network", "ls", "--format", "{{.Name}}").stdout.split())
    out.write(network_failure(area, name, made.stderr or "no reason given", networks).encode())
    return False


def cpu_sets(budgets: dict[str, int], available: set) -> dict[str, tuple[int, ...]]:
    """Assign each concurrent area disjoint CPUs up to its configured CI budget."""
    ordered = sorted(available)
    usable = ordered[:-RESERVED_CPUS] if len(ordered) > RESERVED_CPUS else []
    if len(usable) < len(budgets) or any(count < 1 for count in budgets.values()):
        raise ValueError(f"{len(ordered)} available CPUs cannot give each area its own CPU")
    result = {area: () for area in budgets}
    for area in budgets:
        result[area] = (usable.pop(0),)
    while usable:
        wanting = [area for area, count in budgets.items() if len(result[area]) < count]
        if not wanting:
            break
        for area in wanting:
            if usable:
                result[area] = (*result[area], usable.pop(0))
    return result


def area_budget(job: plan.Job) -> int:
    if job.cpus:
        return job.cpus
    processor = job.env.get("JAVA_TOOL_OPTIONS", "").removeprefix("-XX:ActiveProcessorCount=")
    return int(processor) if processor.isdigit() else 1


def cpu_list(cpus: tuple[int, ...]) -> str:
    return ",".join(str(cpu) for cpu in cpus)


def run_in_image(job: plan.Job, name: str, root: str, mounts: dict, target: str, out, cpus: tuple[int, ...] = None, run=subprocess.run) -> int:
    """The job's steps in its image on a network of its own, once its service is healthy. Nothing outlives the call."""
    cpus = cpus or tuple(sorted(os.sched_getaffinity(0))[:2])
    service = f"{name}-service"

    def docker(*args: str) -> subprocess.CompletedProcess:
        return run(["docker", *args], capture_output=True, text=True)

    try:
        job_limit = len(cpus)
        job_flags = [*docker_budget.container_flags(job_limit, run=run, strict=True), f"--cpuset-cpus={cpu_list(cpus)}"]
        service_flags = [*docker_budget.container_flags(job_limit, run=run, strict=True), f"--cpuset-cpus={cpu_list(cpus)}"] if job.service else []
    except ValueError as error:
        out.write(f"{job.area}: containers failed to start: Docker budget: {error}\n".encode())
        return NOT_STARTED
    try:
        if not _ensure_network(docker, job.area, name, out):
            return NOT_STARTED
        if job.service:
            service_argv = plan.service_argv(job, service, name)
            service_argv[2:2] = service_flags
            started = run(service_argv, capture_output=True, text=True)
            if started.returncode != 0 or not _healthy(docker, service):
                out.write(f"{job.area}: the {job.service['name']} service did not start\n{started.stderr}".encode())
                return NOT_STARTED
            if not _ensure_network(docker, job.area, name, out):
                return NOT_STARTED
        env = {k: os.environ[k] for k in TUNING if k in os.environ and k not in job.env}
        env["CARGO_TARGET_DIR"] = os.path.join(CONTAINER_TARGET, job.area)
        env["CARGO_BUILD_JOBS"] = str(job_limit)
        env["RUST_TEST_THREADS"] = str(job_limit)
        env.update(cache_env(root, mounts))
        os.makedirs(os.path.join(target, job.area), exist_ok=True)
        os.utime(os.path.join(target, job.area))
        job_mounts = mounts.copy()
        if job.warm_volume and "/warm" not in job_mounts.values():
            out.write(f"{job.area}: native warm cache mount is missing\n".encode())
            return NOT_STARTED
        if job.area in ("console", "console-crawl", "deploy") and any("npm ci" in step for step in job.steps):
            modules = os.path.join(target, job.area, "node_modules")
            os.makedirs(modules, exist_ok=True)
            job_mounts[modules] = os.path.join(root, "frontend", "admin", "node_modules")
        argv = plan.job_argv(job, name, name, job_mounts, root, f"{os.getuid()}:{os.getgid()}", env)
        argv[2:2] = job_flags
        return run(argv, stdout=out, stderr=subprocess.STDOUT).returncode
    finally:
        docker("rm", "-f", name, service)
        docker("network", "rm", name)


class Stop:
    """Ends the running areas when one fails. Each running area registers how to kill it; the first failure calls
    every other area's kill once."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tripped = False
        self.kills: dict = {}
        self.killed: set = set()

    def register(self, area: str, kill) -> None:
        with self.lock:
            self.kills[area] = kill
            if self.tripped:
                self.killed.add(area)
                kill()

    def finish(self, area: str) -> None:
        """Forgets an area that ended, so a later failure never signals a process id that may belong to something else."""
        with self.lock:
            self.kills.pop(area, None)

    def trip(self, failed_area: str) -> None:
        with self.lock:
            self.tripped = True
            victims = {a: k for a, k in self.kills.items() if a != failed_area and a not in self.killed}
            self.killed.update(victims)
        for kill in victims.values():
            kill()


def run_on_host(area: str, root: str, target: str, slot: list, out, name: str = "", sccache: str = "",
                job: plan.Job = None, areas: tuple = (), cpus: tuple[int, ...] = None, stop: Stop = None) -> tuple:
    """The area's checks on the host: its exit code and the CPU seconds of everything it started, daemons included
    where systemd can say, and otherwise of what it waited for."""
    cpus = cpus or tuple(sorted(os.sched_getaffinity(0))[:2])
    if os.path.isdir(os.path.join(target, area)):
        os.utime(os.path.join(target, area))
    env = {**os.environ, "CARGO_TARGET_DIR": os.path.join(target, area), "NZELA_AREAS": " ".join(areas), "NZFLEET_BATCH": "1",
           "CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": root,
           "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": os.environ.get("GITHUB_SHA", ""),
           "GITHUB_REPOSITORY": "nzela-org/nzela",
           "GITHUB_API_URL": "https://api.github.com"}
    if job:
        env.update(job.env)
    count = len(cpus)
    env.update(CARGO_BUILD_JOBS=str(count), RUST_TEST_THREADS=str(count))
    if area == "android":
        env["JAVA_TOOL_OPTIONS"] = f"-XX:ActiveProcessorCount={count}"
        env["GRADLE_OPTS"] = f"-Dorg.gradle.workers.max={count}"
    if area == "image":
        env["NZELA_MANAGED_DOCKER"] = "1"
    if area == plan.GATE:
        env["NZELA_GRADLE_SLOT"] = " ".join(slot)
        env.update(rust_cache_env(root, sccache))
    argv = ["taskset", "-c", cpu_list(cpus), *(slot if area == "android" else []), "scripts/checks.sh", area]
    scopes, unit = user_scopes(), f"{name or 'nzf-' + os.path.basename(root)}-{area}"
    if area == "android" and job and not scopes:
        out.write(b"android: native CPU quota needs a managed user scope\n")
        return NOT_STARTED, 0.0
    if scopes:
        if area == "android":
            processor = (job.env if job else {}).get("JAVA_TOOL_OPTIONS", "")
            workers = (job.env if job else {}).get("GRADLE_OPTS", "")
            count = processor.removeprefix("-XX:ActiveProcessorCount=")
            if not count.isdigit() or workers != f"-Dorg.gradle.workers.max={count}":
                out.write(b"android: native CPU and Gradle worker declarations disagree\n")
                return NOT_STARTED, 0.0
        properties = ["--property", f"AllowedCPUs={cpu_list(cpus)}"]
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit, *properties, *argv]
    with tempfile.TemporaryDirectory(prefix="nzela-gh-") as runner_files:
        for channel in ("PATH", "ENV", "OUTPUT", "STEP_SUMMARY"):
            path = os.path.join(runner_files, channel.lower())
            with open(path, "w"):
                pass
            env[f"GITHUB_{channel}"] = path
        with CpuMeter([unit], scopes=scopes) as meter:
            child = subprocess.Popen(argv, cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            if stop:
                # A scope holds daemons that left the process group, so it is stopped as a unit.
                stop.register(area, (lambda: subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True))
                              if scopes else (lambda: os.killpg(child.pid, signal.SIGKILL)))
            _, status, used = os.wait4(child.pid, 0)
            if stop:
                stop.finish(area)
    return os.waitstatus_to_exitcode(status), meter.seconds if scopes else used.ru_utime + used.ru_stime


def run(args: argparse.Namespace) -> int:
    with open(args.workflow) as source:
        workflow = yaml.safe_load(source)
    root = os.path.realpath(args.worktree)
    git_dir = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True).stdout.strip()
    registry = os.path.join(args.cache, "cargo-registry")
    npm = os.path.join(args.cache, "npm")
    mounts = {args.target: CONTAINER_TARGET, registry: "/usr/local/cargo/registry", npm: "/npm"}
    # A repository shared with another checkout is protected read-only; a clone's own .git is in the tree, where a
    # read-only overlay only makes the ownership handback fail.
    if os.path.commonpath([git_dir, root]) != root:
        mounts[git_dir] = git_dir + ":ro"
    for path in (registry, npm):
        os.makedirs(path, exist_ok=True)
    if args.sccache and args.sccache_bin:
        os.makedirs(args.sccache, exist_ok=True)
        mounts.update({args.sccache: "/sccache", os.path.realpath(args.sccache_bin): "/usr/local/bin/sccache:ro"})
    jobs = {area: plan.job_for(workflow, area, {"NZELA_BASE": args.main, "CEILING_BASE": args.main}) for area in args.areas}
    try:
        allocations = cpu_sets({area: area_budget(job) for area, job in jobs.items()}, os.sched_getaffinity(0))
    except (OSError, ValueError) as error:
        print(f"areas: cannot allocate disjoint CPU sets: {error}", file=sys.stderr)
        return NOT_STARTED
    print(f"area CPU sets (2 CPUs reserved): {allocations}", flush=True)
    failed, shown, stats, stop = [], threading.Lock(), {}, Stop()

    def remove_containers(name: str) -> None:
        subprocess.run(["docker", "rm", "-f", name, f"{name}-service"], capture_output=True)

    def one(area: str) -> None:
        job, started = jobs[area], time.time()
        name = f"{args.name or os.path.basename(root)}-{area}"
        if stop.tripped:
            stats[area] = {"rc": STOPPED, "wall_s": 0, "cpu_s": 0.0, "sccache_hits": 0, "sccache_misses": 0, "tests_passed": 0}
            return
        with tempfile.TemporaryFile() as out:
            if job.image:
                stop.register(area, lambda: remove_containers(name))
                area_mounts = mounts.copy()
                if job.warm_volume:
                    warm = os.path.join(args.target, area, "warm")
                    os.makedirs(warm, mode=0o700, exist_ok=True)
                    area_mounts[warm] = "/warm"
                with CpuMeter([name, f"{name}-service"]) as meter:
                    rc = run_in_image(job, name, root, area_mounts, args.target, out, allocations[area])
                cpu = meter.seconds
            else:
                rc, cpu = run_on_host(area, root, args.target, args.slot.split(), out, args.name, args.sccache, job,
                                      areas=tuple(args.areas), cpus=allocations[area], stop=stop)
            out.seek(0)
            text = out.read().decode(errors="replace")
        hits, misses = sccache_counts(text)
        cut_short = rc != 0 and area in stop.killed
        stat = {"rc": STOPPED if cut_short else rc, "wall_s": int(time.time() - started), "cpu_s": round(cpu, 1), "sccache_hits": hits,
                "sccache_misses": misses, "tests_passed": tests_passed(text)}
        with shown:
            stats[area] = stat
            print(f"\n{area}: {'in ' + job.image.split('@')[0] if job.image else 'on the host'}\n{text}", flush=True)
            print(f"{area} took {stat['wall_s']}s", flush=True)
            print(stats_line(area, stat), flush=True)
            if cut_short:
                print(f"{area} stopped: another area failed first", flush=True)
            elif rc != 0:
                failed.append(area)
        if rc != 0 and not cut_short:
            stop.trip(area)

    # The gate goes first and alone: a client that does not compile says so in minutes, before the long areas start,
    # and the build directory it warms is the one the others read.
    gate = [a for a in args.areas if a == plan.GATE]
    rest = [a for a in args.areas if a != plan.GATE]
    for area in gate:
        one(area)
    if not failed:
        with ThreadPoolExecutor(max_workers=max(len(rest), 1)) as pool:
            list(pool.map(one, rest))
    if args.stats:
        json.dump(stats, open(args.stats, "w"))
    print(f"\n{plan.summary(args.areas, sorted(failed))}", flush=True)
    return 1 if failed else 0


def main(argv: list) -> int:
    p = argparse.ArgumentParser(description="run areas as ci runs them")
    p.add_argument("--workflow", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--main", required=True, help="the commit ci's compat job calls the base")
    p.add_argument("--cache", required=True)
    p.add_argument("--name", default="", help="what the batch's containers and networks are named after; the worktree's name by default")
    p.add_argument("--slot", default="")
    p.add_argument("--sccache", default="", help="the compiler cache every Rust build shares; none when empty")
    p.add_argument("--sccache-bin", default="", help="the sccache to mount into the jobs that compile Rust")
    p.add_argument("--stats", default="", help="where to write each area's wall time, CPU time and cache hits")
    p.add_argument("areas", nargs="+")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
