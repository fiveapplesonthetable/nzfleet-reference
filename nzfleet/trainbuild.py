"""The builds `nzfleet train` runs on a merged tree: does it compile, and what do its screenshots look like."""

from __future__ import annotations

import os
import subprocess

from . import batch, config, db

CARGO = "cargo +1.98"
LOG_LINES = 12


def _run(tree: str, command: str, env: dict) -> str:
    """The failing lines of `command`, or '' when it succeeds."""
    done = subprocess.run(command, shell=True, cwd=tree, env=env, capture_output=True, text=True, timeout=3 * 3600)
    if done.returncode == 0:
        return ""
    log = done.stdout + done.stderr
    return batch.excerpt(log) or "\n".join(log.splitlines()[-LOG_LINES:])


def _env(tree: str, name: str, url: str) -> dict:
    return {**os.environ, **config.TOOLCHAIN, "DATABASE_URL": url, "NZELA_GRADLE_SLOT": config.GRADLE_SLOT,
            "ANDROID_HOME": config.ANDROID_HOME, "CARGO_TARGET_DIR": os.path.join(config.TARGETS, name),
            "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_INCREMENTAL": "0"}


def compile_error(tree: str) -> str:
    """Rust (clippy, and the fuzz lock), the console's types, and Android, each as ci compiles them. '' when all build."""
    name = f"train-{os.path.basename(tree)}"
    env = _env(tree, name, db.up(name, tree))
    try:
        for command in (f"{CARGO} clippy --all-targets --locked",
                        f"{CARGO} metadata --manifest-path fuzz/Cargo.toml --locked --format-version 1 >/dev/null",
                        "(cd frontend/admin && npm ci --no-audit --no-fund >/dev/null)",
                        "scripts/checks.sh compile"):
            if error := _run(tree, command, env):
                return error
        return ""
    finally:
        db.remove(name)


ANDROID_SHOTS = "frontend/android/docs/screenshots"
SHOT_LOG = ".nzfleet-train-shots.log"


def _record(command, tree: str, cwd: str, env: dict | None = None, shell: bool = False) -> None:
    """Keeps the full redraw output in the probe tree and raises with its tail on failure."""
    path = os.path.join(tree, SHOT_LOG)
    with open(path, "a") as output:
        output.write(f"$ {command}\n")
        output.flush()
        done = subprocess.run(command, shell=shell, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if done.returncode:
        with open(path) as saved:
            tail = "\n".join(saved.read().splitlines()[-30:])
        detail = f"full output: {path}" + (f"\n{tail}" if tail else "")
        raise subprocess.CalledProcessError(done.returncode, command, output=detail)


def stale_android_shots(tree: str) -> list:
    """Records the Android shots again, as `scripts/checks.sh android` does, and names those that differ from the tree's."""
    redraw(tree, ["android"])
    done = subprocess.run(["git", "status", "--porcelain", "--", ANDROID_SHOTS], cwd=tree, capture_output=True, text=True, check=True)
    return [line[3:] for line in done.stdout.splitlines()]


def redraw(tree: str, areas: list) -> None:
    """Draws the screenshots of `areas` into the tracked directories of `tree`."""
    if "console" in areas:
        _record([os.path.join(batch.CODE, "bin", "redraw-console.sh"), tree], tree, tree)
    if "android" in areas:
        env = {**os.environ, "ANDROID_HOME": config.ANDROID_HOME}
        _record(f"{config.GRADLE_SLOT} ./gradlew testDebugUnitTest --rerun -Proborazzi.test.record=true",
                tree, os.path.join(tree, "frontend/android"), env, shell=True)
