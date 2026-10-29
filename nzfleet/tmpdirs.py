"""Where temporary files go. Tools that read TMPDIR are pointed at one directory, and the JVM tools that ignore it are told separately."""

from __future__ import annotations

import re

IDLE = 4 * 3600
RUNNER_PREFIX = "runner-"
# Leftovers of our own tools in the system temporary directory, from before TMPDIR was set or from a tool that ignores it.
SYSTEM_PREFIXES = ("nzela-e2e.", "nzela-image-binary-", "tmp_", "playwright", ".tmp", "cargo-install", "rustc")


def agent_directory(root: str, key: str) -> str:
    """The temporary directory of the task `key`, named so the tidy pass can tell it from anything else under `root`."""
    return f"{root.rstrip('/')}/{re.sub('[^A-Za-z0-9_.-]', '-', key)}"


def _with_property(existing: str, directory: str) -> str:
    kept = " ".join(part for part in existing.split() if not part.startswith("-Djava.io.tmpdir="))
    return f"{kept} -Djava.io.tmpdir={directory}".strip()


REGISTRY = "-Dorg.gradle.daemon.registry.base="


def with_gradle_registry(opts: str, directory: str) -> str:
    """GRADLE_OPTS naming `directory` as where this process's Gradle daemons register, unless it already names one.

    Daemons share a registry by default, so a build connects to an idle daemon that another scope started, and
    systemd kills it when that scope stops."""
    if REGISTRY in opts:
        return opts
    return f"{opts} {REGISTRY}{directory}".strip()


def environment(directory: str, env: dict) -> dict:
    """`env` with TMPDIR, and the JVM property Gradle, its test workers and Robolectric use instead, set to `directory`.

    JAVA_TOOL_OPTIONS reaches every JVM a build forks, which a gradle.properties jvmargs line does not."""
    return {**env, "TMPDIR": directory, "TMP": directory, "TEMP": directory,
            "JAVA_TOOL_OPTIONS": _with_property(env.get("JAVA_TOOL_OPTIONS", ""), directory),
            "GRADLE_OPTS": with_gradle_registry(env.get("GRADLE_OPTS", ""), f"{directory}/gradle-daemons")}


def runner_dropin(root: str) -> str:
    """A systemd drop-in for the CI runner template: each instance writes under its own directory beside the agents'."""
    return dropin(f"{root.rstrip('/')}/{RUNNER_PREFIX}%i")


def dropin(directory: str) -> str:
    """A systemd drop-in giving a runner unit `directory` as its temporary directory, made before the runner starts."""
    env = environment(directory, {})
    lines = [f'Environment="{key}={value}"' for key, value in env.items()]
    return "[Service]\nExecStartPre=/bin/mkdir -p " + directory + "\n" + "\n".join(lines) + "\n"


def system_leftover(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIXES)
