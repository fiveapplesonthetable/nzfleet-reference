"""Migration files and the migrated template database; standard library only."""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import shlex
import subprocess
import sys

DIR = "crates/nzela/migrations"
NAME = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")


def create(root: str, name: str, now: datetime.datetime) -> str:
    """An empty migration versioned by the UTC second `now`; ValueError if the name or version is unusable."""
    if not NAME.match(name):
        raise ValueError("the name is what the migration makes true, in lower snake case")
    version = now.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    directory = os.path.join(root, DIR)
    taken = sorted(f for f in os.listdir(directory) if f.startswith(version + "_"))
    if taken:
        raise ValueError(f"a migration already has version {version}: {taken[0]}; run this again")
    path = os.path.join(DIR, f"{version}_{name}.sql")
    with open(os.path.join(root, path), "x"):
        pass
    return path


def record(version: int, description: str, checksum: str) -> str:
    """The row sqlx's migrator keeps for an applied migration."""
    return ("insert into _sqlx_migrations (version, description, success, checksum, execution_time) "
            f"values ({version}, '{description.replace('_', ' ')}', true, decode('{checksum}', 'hex'), 0)")


def migrate_template(url: str, directory: str, psql: tuple = ("psql",), stdin: bool = False) -> None:
    """Migrate `template1` on the server `url` names and record each file as sqlx's migrator would.

    `stdin` sends each file over standard input, for a `psql` that cannot see this filesystem.

    A wrong record fails every test at the migrator's own comparison rather than passing quietly.
    """
    template = url.rsplit("/", 1)[0] + "/template1"

    def run(*args: str, source=None) -> None:
        subprocess.run([*psql, template, "-v", "ON_ERROR_STOP=1", "-q", *args],
                       check=True, stdout=subprocess.DEVNULL, stdin=source)

    run("-c", "create table _sqlx_migrations (version bigint primary key, description text not null, "
              "installed_on timestamptz not null default now(), success boolean not null, "
              "checksum bytea not null, execution_time bigint not null)")
    files = sorted((f for f in os.listdir(directory) if f.endswith(".sql")),
                   key=lambda f: int(f.split("_", 1)[0]))
    for file in files:
        path = os.path.join(directory, file)
        with open(path, "rb") as source:
            checksum = hashlib.sha384(source.read()).hexdigest()
        stem = file[: -len(".sql")]
        version, description = stem.split("_", 1)
        if stdin:
            with open(path, "rb") as source:
                run("-f", "-", source=source)
        else:
            run("-f", path)
        run("-c", record(int(version), description, checksum))


def main(argv: list) -> int:
    """`new <name>` or `template <database url> [dir]`."""
    if len(argv) == 2 and argv[0] == "new":
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                              capture_output=True, text=True).stdout.strip()
        try:
            print(create(root, argv[1], datetime.datetime.now(datetime.timezone.utc)))
        except ValueError as error:
            print(f"usage: nzfleet-migration new <snake_case_name>\n  {error}", file=sys.stderr)
            return 2
        return 0
    if argv[:1] == ["template"] and len(argv) in (2, 3):
        try:
            remote = shlex.split(os.environ.get("NZFLEET_PSQL", ""))
            migrate_template(argv[1], argv[2] if len(argv) == 3 else DIR,
                             tuple(remote) or ("psql",), stdin=bool(remote))
        except (subprocess.CalledProcessError, OSError) as error:
            print(f"migrated template: {error}", file=sys.stderr)
            return 1
        return 0
    print("usage: nzfleet-migration new <name> | template <database url> [migrations dir]", file=sys.stderr)
    return 2
