"""A Postgres of an agent's own, with every migration of its checkout applied."""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import time
from . import docker_budget, plan
from .config import PSQL
from .world import Failed

IMAGE = "postgres:16"
PASSWORD = "nzela"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _psql(url: str, *args: str) -> subprocess.CompletedProcess:
    # client_min_messages keeps the migrations' notices out of the output.
    return subprocess.run([PSQL, url, *args], capture_output=True, text=True, env=dict(os.environ, PGOPTIONS="-c client_min_messages=warning"))


def up(name: str, tree: str, port: int = 0) -> str:
    """Starts container `agentdb-<name>` afresh, migrates it from `tree`, and returns its DATABASE_URL."""
    container, port = f"agentdb-{name}", port or free_port()
    try:
        managed = os.environ.get("NZELA_MANAGED_DOCKER") == "1"
        budget_flags = docker_budget.container_flags(2, strict=managed, prefer_owner=not managed)
    except ValueError as error:
        raise Failed(f"Docker budget: {error}") from error
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    # fsync off and a RAM disk: this database is thrown away, and the disk under the checkouts is the slow part.
    run = subprocess.run(
        ["docker", "run", "-d", "--init", *budget_flags, "--name", container, "-p", f"127.0.0.1:{port}:5432", "--tmpfs", "/var/lib/postgresql/data:rw,size=4g",
         "-e", f"POSTGRES_PASSWORD={PASSWORD}", IMAGE, "-c", "fsync=off", "-c", "synchronous_commit=off",
         "-c", "full_page_writes=off", "-c", "max_connections=300"], capture_output=True, text=True)
    if run.returncode != 0:
        raise Failed(f"docker could not start {container}: {run.stderr.strip()[:200]}. Is the docker daemon running (docker info)?")
    admin = f"postgres://postgres:{PASSWORD}@127.0.0.1:{port}/postgres"
    while _psql(admin, "-qc", "select 1").returncode != 0:
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True)
        if state.stdout.strip() != "true":
            raise Failed(f"{container} stopped before it accepted connections. Look at: docker logs {container}")
        time.sleep(0.5)
    created = _psql(admin, "-qc", "create database nzela")
    if created.returncode != 0:
        raise Failed(f"could not create the database: {created.stderr.strip()[:200]}")
    url = f"postgres://postgres:{PASSWORD}@127.0.0.1:{port}/nzela"
    for path in plan.migration_order(glob.glob(os.path.join(tree, "crates", "*", "migrations", "*.sql"))):
        done = _psql(url, "-v", "ON_ERROR_STOP=1", "-q", "-f", path)
        if done.returncode != 0:
            raise Failed(f"migration {os.path.relpath(path, tree)} failed: {done.stderr.strip()[:300]}")
    return url


def remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", f"agentdb-{name}"], capture_output=True)
