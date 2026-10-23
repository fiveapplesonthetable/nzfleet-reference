"""Docker container limits for the selected daemon."""

import argparse
import json
import os
from pathlib import Path
import subprocess

OWNER_SLICE = "user-1000.slice"
OWNER_CONFIG = Path(__file__).resolve().parents[1] / "bin/root/user-1000.slice.conf"


def local_root_daemon(endpoint: str, security: list[str]) -> bool:
    """Whether the selected socket is the local rootful Docker daemon."""
    if not isinstance(endpoint, str) or not isinstance(security, list) or not all(isinstance(item, str) for item in security):
        raise ValueError("Docker did not report a valid endpoint and security options")
    if not endpoint.startswith("unix://") or "name=rootless" in security:
        return False
    return Path(endpoint[len("unix://"):]).resolve() == Path("/run/docker.sock").resolve()


def flags(endpoint: str, driver: str, security: list[str], cpu_max: str, limit: int,
          owner_quota: int, strict: bool = False) -> list[str]:
    """Return CPU limits, requiring the owner parent only for managed jobs."""
    if limit <= 0:
        raise ValueError("Docker CPU limit must be positive")
    if not isinstance(driver, str) or not isinstance(cpu_max, str):
        raise ValueError("Docker did not report a valid cgroup driver or owner quota")
    if not local_root_daemon(endpoint, security) or not strict:
        return [f"--cpus={limit}"]
    if driver != "systemd":
        raise ValueError("local root Docker needs the systemd cgroup driver for user-1000.slice; configure Docker or use rootless Docker")
    parts = cpu_max.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts) or int(parts[1]) == 0:
        raise ValueError("user-1000.slice needs a finite CPU quota before local root Docker jobs start")
    if int(parts[0]) > owner_quota * int(parts[1]):
        raise ValueError(f"user-1000.slice quota exceeds the configured {owner_quota} CPUs; apply the partition before starting Docker jobs")
    return [f"--cgroup-parent={OWNER_SLICE}", f"--cpus={limit}"]


def configured_owner_quota(path=OWNER_CONFIG) -> int:
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise ValueError(f"cannot read owner CPU quota from {path}: {error}") from error
    for line in lines:
        if line.startswith("CPUQuota=") and line.endswith("%"):
            try:
                percent = int(line[len("CPUQuota="):-1])
            except ValueError as error:
                raise ValueError(f"invalid owner CPU quota in {path}") from error
            if percent > 0 and percent % 100 == 0:
                return percent // 100
    raise ValueError(f"invalid owner CPU quota in {path}")


def selected_endpoint(run=subprocess.run) -> str:
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        return os.environ["DOCKER_HOST"]
    context = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                  capture_output=True, text=True)
    if context.returncode:
        raise ValueError(f"cannot inspect selected Docker context: {context.stderr.strip()}")
    return context.stdout.strip()


def container_flags(limit: int, run=subprocess.run, cgroup=Path("/sys/fs/cgroup/user.slice/user-1000.slice/cpu.max"),
                    strict: bool = False, prefer_owner: bool = False) -> list[str]:
    info = run(["docker", "info", "--format", "{{json .CgroupDriver}} {{json .SecurityOptions}}"],
               capture_output=True, text=True)
    if info.returncode:
        raise ValueError(f"cannot inspect Docker daemon: {info.stderr.strip()}")
    try:
        decoder = json.JSONDecoder()
        driver, offset = decoder.raw_decode(info.stdout.strip())
        security, remainder = decoder.raw_decode(info.stdout.strip()[offset:].strip())
        if info.stdout.strip()[offset:].strip()[remainder:].strip():
            raise ValueError("extra Docker info")
    except (ValueError, TypeError) as error:
        raise ValueError("Docker did not report its cgroup driver and security options") from error
    endpoint = selected_endpoint(run)
    if not isinstance(driver, str):
        raise ValueError("Docker did not report a valid cgroup driver")
    managed = (strict or prefer_owner) and local_root_daemon(endpoint, security)
    try:
        cpu_max = cgroup.read_text().strip() if managed and cgroup.exists() else ""
        owner_quota = configured_owner_quota() if managed else 0
        return flags(endpoint, driver, security, cpu_max, limit, owner_quota, strict=managed)
    except OSError as error:
        if not prefer_owner or strict:
            raise ValueError(f"cannot read owner CPU quota from {cgroup}: {error}") from error
    except ValueError:
        if not prefer_owner or strict:
            raise
    return flags(endpoint, driver, security, "", limit, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpus", type=int, required=True)
    parser.add_argument("--managed", action="store_true")
    parser.add_argument("--prefer-owner", action="store_true")
    args = parser.parse_args()
    try:
        print(" ".join(container_flags(args.cpus, strict=args.managed, prefer_owner=args.prefer_owner)))
    except ValueError as error:
        parser.exit(1, f"Docker budget: {error}\n")


if __name__ == "__main__":
    main()
