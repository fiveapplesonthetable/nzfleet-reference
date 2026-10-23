#!/usr/bin/env python3
"""Validate the host CPU partition before root installs it."""

import argparse
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
BIN = ROOT.parent
WORKFLOW = ROOT.parents[3] / ".github/workflows/ci.yaml"
CHECKS = ROOT.parents[3] / "scripts/areas/image.sh"


def cpus(spec):
    result = set()
    for part in spec.strip().split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"invalid CPU list: {spec}")
        first = int(match[1])
        last = int(match[2] or first)
        if last < first or result.intersection(range(first, last + 1)):
            raise ValueError(f"invalid CPU list: {spec}")
        result.update(range(first, last + 1))
    if not result:
        raise ValueError("empty CPU list")
    return result


def quota(path):
    match = re.search(r"^CPUQuota=(\d+)%$", path.read_text(), re.MULTILINE)
    if not match or int(match[1]) % 100:
        raise ValueError(f"quota must be whole CPUs: {path}")
    return int(match[1]) // 100


def configured_quotas():
    return {
        "trusted": quota(ROOT / "user-1001.slice.conf"),
        "owner": quota(ROOT / "user-1000.slice.conf"),
        "pr": quota(ROOT / "user-1002.slice.conf"),
        "batch": quota(BIN / "nzfleet-batch.slice"),
        "agent": quota(BIN / "nzfleet-agent.slice"),
    }


def job_section(workflow, job):
    match = re.search(rf"(?ms)^  {job}:\n(.*?)(?=^  [a-z][a-z-]*:|\Z)", workflow)
    if not match:
        raise ValueError(f"missing CI job: {job}")
    return match[1]


def job_cpus(workflow, job):
    section = job_section(workflow, job)
    container = section.split("    services:", 1)[0].split("    steps:", 1)[0]
    match = re.search(r"options: \$\{\{ vars.CI_RUNS_ON && '--cpus=(\d+)' \|\| '' \}\}", container)
    if not match:
        raise ValueError(f"{job} lacks a self-hosted CPU cap")
    return int(match[1])


def android_cpus(workflow):
    section = job_section(workflow, "android")
    processor = re.search(r"ActiveProcessorCount=(\d+)", section)
    workers = re.search(r"org.gradle.workers.max=(\d+)", section)
    if not processor or not workers or processor[1] != workers[1]:
        raise ValueError("Android processor view and Gradle workers must have one bounded CPU budget")
    return int(processor[1])


def postgres_cpus(workflow, job):
    section = job_section(workflow, job)
    if not re.search(r"(?m)^    services:\n      postgres:", section):
        raise ValueError(f"{job} lacks its Postgres service")
    match = re.search(r"vars.CI_RUNS_ON && '--cpus=(\d+)'", section.split("    services:", 1)[1].split("    steps:", 1)[0])
    if not match:
        raise ValueError(f"{job} Postgres lacks a self-hosted CPU cap")
    return int(match[1])


def validate(host, trusted_affinity, shared_affinity, workflow, quotas, reserves=4):
    if trusted_affinity & shared_affinity or trusted_affinity | shared_affinity != host:
        raise ValueError("CPU affinity domains overlap or omit host CPUs")
    trusted = quotas["trusted"]
    owner = quotas["owner"]
    pr = quotas["pr"]
    batch = quotas["batch"]
    agent = quotas["agent"]
    if trusted > len(trusted_affinity) or owner + pr > len(shared_affinity):
        raise ValueError("CPU quota exceeds effective affinity; lower the slice quotas or assign more effective CPUs")
    if max(batch, agent) > owner:
        raise ValueError("batch or agent quota exceeds their shared owner slice ceiling")
    if trusted + owner + pr + reserves > len(host):
        raise ValueError("aggregate quotas leave too little system headroom; lower configured quotas or reserve")
    rust = [job_cpus(workflow, job) for job in ("check", "compat", "e2e")]
    console = job_cpus(workflow, "console-browser")
    console_static = job_cpus(workflow, "console")
    android = android_cpus(workflow)
    image_match = re.search(r"docker_budget --cpus (\d+)", CHECKS.read_text())
    if not image_match:
        raise ValueError("release image CPU budget is not declared")
    image = int(image_match[1])
    image_section = job_section(workflow, "image")
    if "needs: select" not in image_section or "needs: [select, android]" in image_section:
        raise ValueError("release image must run in parallel with Android")
    device_section = job_section(workflow, "android-device")
    if ("needs: [select, check, compat, e2e, console, console-browser, android, image]" not in device_section
            or "!cancelled()" not in device_section
            or "ActiveProcessorCount=4" not in device_section
            or "org.gradle.workers.max=4" not in device_section):
        raise ValueError("device work must wait for heavy CI and use a four-core Gradle view")
    for job, cap in zip(("check", "compat", "e2e"), rust):
        section = job_section(workflow, job)
        for key in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS"):
            if f'{key}: "{cap}"' not in section:
                raise ValueError(f"{job} {key} does not match its container quota")
    rust_postgres = postgres_cpus(workflow, "check")
    postgres_service = job_section(workflow, "check").split("    services:", 1)[1].split("    steps:", 1)[0]
    memory = re.search(r"--memory=(\d+)g\b", postgres_service)
    if rust_postgres < 4 or not memory or int(memory[1]) < 8:
        raise ValueError("Rust Postgres needs at least four CPUs and eight GiB for parallel database lifecycles")
    rust_peak = rust[0] + rust_postgres
    services = sum(postgres_cpus(workflow, job) for job in ("compat", "e2e"))
    light = sum(job_cpus(workflow, job) for job in ("contract", "secrets", "site", "deploy", "fleet"))
    for job in ("contract", "secrets", "site", "deploy", "fleet"):
        if job_cpus(workflow, job) != 1:
            raise ValueError(f"{job} exceeds its light-job budget")
    if console < 2 or console_static < 1 or android < 1:
        raise ValueError("browser or Android CPU budget is too small")
    if any("NZELA_CONSOLE_WORKERS" in job_section(workflow, job)
           for job in ("console", "console-browser")):
        raise ValueError("console browser workers must come from the container's CPU quota, not be pinned")
    # The suite's four browsers are a cgroup-capped burst that starts after the static
    # job; they may take a third of the slice and are not summed with the rest.
    peak = rust_peak + sum(rust[1:]) + services + console_static + light + android + image
    if peak > trusted:
        raise ValueError(f"CI jobs, Android, image and Postgres need {peak} CPUs, trusted parent has {trusted}")
    if console * 3 > trusted:
        raise ValueError(f"console browser cap {console} exceeds a third of the trusted parent {trusted}")
    return trusted, owner, pr, batch, agent, tuple(rust), console, console_static, android, image, services, light


def range_string(values):
    ordered = sorted(values)
    spans = []
    start = end = ordered[0]
    for value in ordered[1:]:
        if value == end + 1:
            end = value
        else:
            spans.append(f"{start}-{end}" if start != end else str(start))
            start = end = value
    spans.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(spans)


def plan(host, workflow, quotas, reserves=4):
    trusted_count = quotas["trusted"]
    ordered = sorted(host)
    trusted_affinity = set(ordered[:trusted_count])
    shared_affinity = set(ordered[trusted_count:])
    limits = validate(host, trusted_affinity, shared_affinity, workflow, quotas, reserves)
    return trusted_affinity, shared_affinity, limits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-cpus")
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    parser.add_argument("--emit-shell", action="store_true")
    parser.add_argument("--configured", action="store_true", help="validate the configured partition against its declared CPU budget")
    parser.add_argument("--system-reserve", type=int, default=int(os.environ.get("NZELA_SYSTEM_CPU_RESERVE", "4")))
    args = parser.parse_args()
    if args.system_reserve < 1:
        parser.error("system reserve must be positive")
    try:
        quotas = configured_quotas()
        if args.configured:
            configured_host_size = quotas["trusted"] + quotas["owner"] + quotas["pr"] + args.system_reserve
            host = set(range(configured_host_size))
        else:
            host_spec = args.host_cpus or Path("/sys/fs/cgroup/cpuset.cpus.effective").read_text().strip()
            host = cpus(host_spec)
        trusted, shared, limits = plan(host, args.workflow.read_text(), quotas, args.system_reserve)
    except ValueError as error:
        parser.error(str(error))
    if args.emit_shell:
        print(f"TRUSTED_CPUS={range_string(trusted)}")
        print(f"SHARED_CPUS={range_string(shared)}")
    else:
        print(f"trusted={range_string(trusted)} shared={range_string(shared)} quotas={limits}")


if __name__ == "__main__":
    main()
