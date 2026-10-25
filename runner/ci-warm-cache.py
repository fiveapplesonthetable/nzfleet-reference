#!/usr/bin/env python3
"""Provision and maintain one private self-hosted CI cache root."""

import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

BASE = Path('/srv/fleet/nzfleet/ci-cache')
BASE_OWNER = 0
MOUNT = Path('/warm')
PULL_REQUEST_ROOT = re.compile(r'/srv/fleet/nzela-pr/ci-cache/repo-[1-9][0-9]*')
AREAS = ('rust', 'compat', 'e2e')
SCAN_SLOTS = 2
SLOT_LIMIT = 12 * 1024**3
TOTAL_LIMIT = 32 * 1024**3


def exact_root(repository_id):
    if not re.fullmatch(r'[1-9][0-9]*', repository_id):
        raise ValueError('repository ID must be a positive decimal number')
    return BASE / f'repo-{repository_id}'


def directory(path, owner=None, mode=None):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'{path}: expected a directory, not a symlink or file')
    if owner is not None and info.st_uid != owner:
        raise ValueError(f'{path}: owner UID {info.st_uid}, expected {owner}')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError(f'{path}: mode {stat.S_IMODE(info.st_mode):04o}, expected {mode:04o}')
    return info


def provision(root, owner):
    if os.geteuid() != 0:
        raise ValueError('provision needs root; run the printed sudo command')
    directory(BASE.parent.parent)
    directory(BASE.parent)
    if not BASE.exists() and not BASE.is_symlink():
        BASE.mkdir(mode=0o711)
    directory(BASE)
    if BASE.stat().st_uid != BASE_OWNER or stat.S_IMODE(BASE.stat().st_mode) != 0o711:
        raise ValueError(f'{BASE}: expected root-owned mode 0711; repair this parent explicitly')
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
        os.chown(root, owner, owner)
    info = directory(root)
    if info.st_uid != owner:
        if any(root.iterdir()):
            raise ValueError(f'{root}: wrong owner and nonempty; inspect it before changing ownership')
        os.chown(root, owner, owner)
    os.chmod(root, 0o700)
    verify(root, owner)


def verify(root, owner):
    directory(BASE.parent.parent)
    directory(BASE.parent)
    directory(BASE, BASE_OWNER, 0o711)
    directory(root, owner, 0o700)


def mounted():
    path = MOUNT
    directory(path)
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError('/warm is not writable by the job; provision the selected cache for the runner pool UID')
    override = os.environ.get('CI_WARM_OVERRIDE', '')
    if PULL_REQUEST_ROOT.fullmatch(override) and not any(path.iterdir()) and stat.S_IMODE(path.lstat().st_mode) == 0o755:
        # Docker makes a missing bind source mode 0755. This one sits in the pull-request account's private home, so
        # its first job tightens it; any other absolute path that arrives public is still refused.
        path.chmod(0o700)
    if (os.environ.get('CI_SELF_HOSTED') and not override) or override.startswith('/'):
        directory(path, 0, 0o700)
    print('warm cache mount is writable; default self-hosted mount is private'
          if os.environ.get('CI_SELF_HOSTED') and not override else 'warm cache mount is writable')


def size(path):
    result = subprocess.run(['du', '-sx', '--block-size=1', '--', str(path)], capture_output=True, text=True, check=True)
    return int(result.stdout.split()[0])


def candidates(root):
    verify(root, root.stat().st_uid)
    target = root / 'target'
    found = []
    if target.exists() or target.is_symlink():
        directory(target, root.stat().st_uid)
        for area in AREAS:
            for slot in range(1, SCAN_SLOTS + 1):
                found.extend(candidate(target / f'{area}-{slot}', target / f'{area}-{slot}.lock', root.stat().st_uid))
    found.extend(candidate(root / 'typeshare', root / 'typeshare.lock', root.stat().st_uid))
    return found


def candidate(path, lock, owner):
    if not path.exists() and not path.is_symlink():
        return []
    directory(path, owner)
    if not lock.is_file() or lock.is_symlink():
        raise ValueError(f'{lock}: missing or unsafe lock; preserving cache')
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    if os.fstat(fd).st_uid != owner:
        os.close(fd)
        raise ValueError(f'{lock}: wrong owner; preserving cache')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return [(path, None, None, None)]
    return [(path, size(path), path.stat().st_mtime_ns, fd)]


def maintenance(root, delete=False):
    verify(root, root.stat().st_uid)
    actual = size(root)
    for entry in sorted(root.iterdir()):
        if entry.name not in ('target', 'typeshare'):
            print(f'{entry.name}: {size(entry)} bytes (retained)')
    print(f'whole cache: {actual} bytes; limit: {TOTAL_LIMIT} bytes')
    found = candidates(root)
    occupied = [entry for entry in found if entry[1] is None]
    if occupied:
        print(f'{len(occupied)} active cache(s) preserved; no deletion')
        for _, _, _, fd in found:
            if fd is not None:
                os.close(fd)
        if actual > TOTAL_LIMIT:
            raise ValueError(f'whole cache {actual} bytes exceeds {TOTAL_LIMIT} bytes with active locks; no deletion')
        return
    retained = actual
    selected = set()
    for path, bytes_used, _, _ in found:
        if bytes_used is not None and bytes_used > SLOT_LIMIT:
            selected.add(path)
            retained -= bytes_used
    for path, bytes_used, _, _ in sorted((entry for entry in found if entry[1] is not None and entry[0] not in selected), key=lambda entry: entry[2]):
        if retained <= TOTAL_LIMIT:
            break
        selected.add(path)
        retained -= bytes_used
    for path, bytes_used, _, fd in found:
        if path in selected:
            print(f'{"remove" if delete else "would remove"} {path} ({bytes_used} bytes, idle lock held)')
            if delete:
                shutil.rmtree(path)
        if fd is not None:
            os.close(fd)
    actual_retained = size(root) if delete else actual
    print(f'whole retained: {actual_retained} bytes; projected after target removal: {retained} bytes')
    if (actual_retained if delete else retained) > TOTAL_LIMIT:
        raise ValueError(f'whole cache {actual_retained if delete else retained} bytes exceeds {TOTAL_LIMIT} bytes; non-target data is preserved')


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('provision', 'verify', 'plan', 'prune'):
        command = commands.add_parser(name)
        command.add_argument('--repository-id', required=True)
        command.add_argument('--owner-uid', type=int, required=True)
    commands.add_parser('mounted')
    args = parser.parse_args()
    try:
        if args.command == 'mounted':
            mounted()
            return 0
        root = exact_root(args.repository_id)
        if args.owner_uid < 1:
            raise ValueError('owner UID must be positive')
        if args.command == 'provision':
            provision(root, args.owner_uid)
        elif args.command == 'verify':
            verify(root, args.owner_uid)
        else:
            verify(root, args.owner_uid)
            maintenance(root, args.command == 'prune')
        print(f'{root}: {args.command} complete')
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'ci-warm-cache: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
