"""A slot's build directory holds one directory per area; when it outgrows its cap the least recently used areas go first."""

from __future__ import annotations

import os
import shutil


def _bytes(path: str) -> int:
    seen = set()
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += info.st_size
    return total


def shrink(target: str, max_gb: int) -> list:
    """Removes the oldest area directories of `target` until it is within `max_gb`; the names removed, oldest first."""
    try:
        areas = [e for e in os.scandir(target) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    sizes = {e.name: _bytes(e.path) for e in areas}
    removed = []
    for entry in sorted(areas, key=lambda e: e.stat().st_mtime):
        if sum(sizes.values()) <= max_gb * 2**30:
            break
        shutil.rmtree(entry.path, ignore_errors=True)
        sizes[entry.name] = 0
        removed.append(entry.name)
    return removed
