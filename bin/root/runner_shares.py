#!/usr/bin/env python3
"""Install per-runner CPU quotas beneath the configured CI account slices."""

import argparse
import os
from pathlib import Path
import pwd

from resource_budget import ROOT, quota


def shares(total, names):
    """Divide a slice's CPU quota across its registered one-job runners."""
    if not names or total < len(names):
        raise ValueError("runner count exceeds the CI slice CPU quota")
    each, extra = divmod(total, len(names))
    return {name: each + (index < extra) for index, name in enumerate(sorted(names))}


def registered(home):
    root = home / "actions-runner"
    return [entry.name for entry in root.iterdir() if entry.is_dir() and (entry / ".runner").is_file()] if root.is_dir() else []


def install(uid, slice_file, check=False):
    if os.geteuid() != uid:
        raise ValueError("runner drop-ins must be written as their runner account")
    home = Path(pwd.getpwuid(uid).pw_dir)
    names = registered(home)
    if not names:
        return
    directory = home / ".config/systemd/user"
    allocation = shares(quota(ROOT / slice_file), names)
    for name, cores in allocation.items():
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid runner instance: {name}")
        dropin = directory / f"nzela-runner@{name}.service.d/50-cpu-share.conf"
        if not check:
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text(f"[Service]\nCPUQuota={cores * 100}%\n")
        print(f"{dropin}: {cores} CPUs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--slice-file", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    install(args.uid, args.slice_file, args.check)
