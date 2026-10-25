"""The system disk, which nothing of ours may fill. The owner hears about it well before it is full."""

from __future__ import annotations

WARN_PERCENT = 80
LOW_FREE_GB = 20
PRUNE_BELOW_GB = 40
ROOT_PRUNE = "/usr/local/sbin/nzela-runner-prune"
TITLE = "The root disk is filling"


def used_percent(total: int, free: int) -> int:
    """Whole percent of `total` in use, rounded up so a disk is never reported emptier than it is."""
    return 0 if total <= 0 else -(-100 * (total - free) // total)


def warning(percent: int, free_gb: int) -> str | None:
    """The sentence for the owner once `/` is past the warning line or under 20 GB free, or None while it is comfortable."""
    if percent < WARN_PERCENT and free_gb >= LOW_FREE_GB:
        return None
    return (f"/ is {percent}% full with {free_gb} GB free. Nothing of ours should write there: look for temporary files "
            f"outside NZFLEET_TMP (`du -xh / --max-depth=2` as a person, never under /home).")
