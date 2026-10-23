"""The main checkout must stay a work tree: git refuses every work-tree command once `core.bare` is true."""

from __future__ import annotations

from . import world


def is_bare(configured: str) -> bool:
    return configured.strip().lower() in ("true", "yes", "on", "1")


def repair(dry_run: bool) -> str | None:
    """Sets `core.bare` back to false when something has flipped it; returns the line to log, or None when it was fine."""
    value = world.git("config", "--local", "--get", "core.bare", check=False)
    if not is_bare(value):
        return None
    if dry_run:
        return "core.bare is true on the main checkout; a tick would set it back to false"
    world.git("config", "--local", "core.bare", "false")
    return "core.bare was true on the main checkout, which breaks every git command in it; set back to false. Find what set it"
