#!/usr/bin/env python3
"""Install per-runner CPU quotas beneath the configured CI account slices."""

import argparse
import os
from pathlib import Path
import pwd
import sys

from resource_budget import ROOT, WORKFLOW, heaviest_job, quota

sys.path.insert(0, str(ROOT.parents[1]))
from nzfleet import tmpdirs


def shares(total, names, heaviest):
    """Each runner may use what the heaviest job declares, bounded by its slice.

    The slice quota is what holds all the runners of a tier together; dividing it
    between them left a lone job a fraction of the CPUs its container asked for.
    """
    if not names:
        raise ValueError("no registered runner")
    return {name: min(total, heaviest) for name in sorted(names)}


def registered(home):
    root = home / "actions-runner"
    return [entry.name for entry in root.iterdir() if entry.is_dir() and (entry / ".runner").is_file()] if root.is_dir() else []


def install(uid, slice_file, check=False, tmp_root=None):
    if os.geteuid() != uid:
        raise ValueError("runner drop-ins must be written as their runner account")
    home = Path(pwd.getpwuid(uid).pw_dir)
    names = registered(home)
    if not names:
        return
    directory = home / ".config/systemd/user"
    allocation = shares(quota(ROOT / slice_file), names, heaviest_job(WORKFLOW.read_text()))
    for name, cores in allocation.items():
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid runner instance: {name}")
        dropin = directory / f"nzela-runner@{name}.service.d/50-cpu-share.conf"
        if not check:
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text(f"[Service]\nCPUQuota={cores * 100}%\n")
        print(f"{dropin}: {cores} CPUs")
        if tmp_root:
            temporary = dropin.with_name("50-tmp.conf")
            if not check:
                temporary.write_text(tmpdirs.dropin(f"{tmp_root.rstrip('/')}/{pwd.getpwuid(uid).pw_name}/runner-{name}"))
            print(f"{temporary}: temporary files under {tmp_root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--slice-file", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tmp-root", help="directory whose per-account subdirectory holds the runners' temporary files")
    args = parser.parse_args()
    install(args.uid, args.slice_file, args.check, args.tmp_root)
