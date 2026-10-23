"""Refuses to import the test suite against the fleet's live NZFLEET_HOME, which a test would overwrite with fixtures."""

import os


class LiveHomeRefused(RuntimeError):
    pass


def live_homes(declared: dict) -> set:
    """Where the running fleet keeps its state: what the env file names, and the default."""
    named = declared.get("NZFLEET_HOME") or os.path.expanduser("~/.nzfleet")
    return {os.path.realpath(named), os.path.realpath(os.path.expanduser("~/.nzfleet"))}


def check(home: str, declared: dict, environ: dict) -> None:
    if environ.get("NZFLEET_LIVE"):
        return
    if os.path.realpath(home) in live_homes(declared):
        raise LiveHomeRefused(f"the tests would write into the live NZFLEET_HOME {home}; run them with NZFLEET_HOME=$(mktemp -d)")
