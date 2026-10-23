"""`nzfleet setup`: brings a machine to the state nzfleet needs, and says what is still missing. Safe to run again."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from . import config, selfupdate, statuses, units

BIN = units.BIN
LINKED = ("nzfleet", "agent-db", "gradle-slot")
POOL_BASE, POOL_SIZE, POOL_NETWORKS = "10.210.0.0/16", 24, 256
HOOKS = ("agent-guard.py", "bash-guard.py", "context-cap.py")
ROOT_PRUNE = "/usr/local/sbin/nzela-runner-prune"
ROOT_LINE = "7 * * * * timeout 10m flock -n /run/lock/nzela-runner-prune.lock /usr/local/sbin/nzela-runner-prune scheduled"


@dataclass(frozen=True)
class Need:
    name: str
    argv: tuple
    accepts: object  # (exit code, output) -> bool
    fix: str


def needs() -> tuple:
    adb = os.environ.get("NZFLEET_ADB_SERVER", "")
    found = [
        Need("docker", ("docker", "info"), lambda rc, out: rc == 0, "install Docker, start its daemon, and add this user to the docker group"),
        Need("rustup 1.98", ("rustup", "toolchain", "list"), lambda rc, out: rc == 0 and "1.98" in out,
             "rustup toolchain install 1.98 --profile minimal -c clippy -c rustfmt"),
        Need("node 22", ("node", "--version"), lambda rc, out: rc == 0 and out.startswith("v22."), "install Node 22 and put it on PATH in the env file"),
        Need("Android SDK", ("test", "-d", os.path.join(config.ANDROID_HOME or "/nonexistent", "platforms")), lambda rc, out: rc == 0,
             "install the SDK and set ANDROID_HOME in the env file"),
        Need("gcloud", (config.GCLOUD, "--version"), lambda rc, out: rc == 0, "install the Google Cloud CLI or set NZFLEET_GCLOUD"),
        Need("gh signed in", ("gh", "auth", "status"), lambda rc, out: rc == 0, "gh auth login, as the account in NZFLEET_GIT_NAME"),
    ]
    for name in HOOKS:
        found.append(Need(f"hook {name}", ("test", "-x", os.path.join(config.REPO, ".claude", "hooks", name)), lambda rc, out: rc == 0,
                          "fetch upstream main: .claude/hooks and .claude/settings.json ship in the repository"))
    if adb:
        found.append(Need("phones' adb host", ("adb", "devices"), lambda rc, out: rc == 0, f"reach the adb server at {adb}"))
    return tuple(found)


def address_pool_advice(info: str) -> list:
    """What to tell the owner when the daemon runs on docker's default address pools, which allow about 31
    networks; nothing when pools are set or the answer is not one we recognise."""
    if info.strip() not in ("null", "[]"):
        return []
    return [
        "docker is on its default address pools, which allow about 31 networks; every area of every batch makes one.",
        f"Give it room for {POOL_NETWORKS}: merge this into /etc/docker/daemon.json",
        f'  {{"default-address-pools": [{{"base": "{POOL_BASE}", "size": {POOL_SIZE}}}]}}',
        "then `sudo systemctl restart docker`. That restarts docker and every container on it, so do it when no batch or agent database is running.",
        "setup does not apply it.",
    ]


def render_env(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    return template


def missing_variables(template: str, env_text: str) -> list:
    """Variables the template documents that an existing env file never mentions, set or commented."""
    documented = {line.lstrip("#").partition("=")[0] for line in template.splitlines() if line[:1] != "#" or line[1:2].isupper()}
    return sorted(name for name in documented if name.isidentifier() and name.isupper() and f"{name}=" not in env_text)


def partition_advice(shown: str) -> list:
    """Print the source plan and the separate root step for owner review."""
    return [
        f"CI partition currently reports {shown.strip() or 'unknown'}; review before applying while CI is idle:",
        f"  python3 {os.path.join(BIN, 'root', 'resource_budget.py')}",
        f"  sudo sh {os.path.join(BIN, 'root', 'partition.sh')}",
    ]


def run_tool(argv: tuple, env: dict | None = None) -> tuple:
    """(exit code, output) of `argv`; 127 when it cannot run."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return done.returncode, done.stdout + done.stderr


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _fresh_link(target: str, directory: str) -> str:
    """A new symlink beside the old one, so it can replace it in one step."""
    temp = os.path.join(directory, f".link-{os.getpid()}")
    if os.path.lexists(temp):
        os.remove(temp)
    os.symlink(target, temp)
    return temp


def _link(target: str, name: str, directory: str) -> str:
    path = os.path.join(directory, name)
    if os.path.islink(path) and os.path.realpath(path) == os.path.realpath(target):
        return ""
    if os.path.islink(path):
        os.replace(_fresh_link(target, directory), path)
        return f"repointed {path} to {target}"
    if os.path.lexists(path):
        return f"{path} exists and is not a link to {target}; left alone"
    os.makedirs(directory, exist_ok=True)
    os.symlink(target, path)
    return f"linked {path}"


def warm_directories() -> tuple:
    """Where batches keep what they reuse; each is made if absent."""
    return (config.WARM, config.SCCACHE, os.path.join(config.CACHE, "npm"), os.path.join(config.CACHE, "cargo-registry"),
            os.path.join(config.HOME, "bin"))


def ci_runner_steps(repo: str) -> list:
    """What to run once, by hand, so the CI runners keep their compiler cache and build directories between jobs."""
    helper = os.path.join(repo, "scripts", "ci-warm-cache.py")
    return [
        "Self-hosted default caches need private rootless-Docker ownership before CI uses them:",
        f"  sudo python3 {helper} provision --repository-id \"$(gh api repos/nzela-org/nzela --jq .id)\" --owner-uid 1001",
        f"  sudo python3 {helper} verify --repository-id \"$(gh api repos/nzela-org/nzela --jq .id)\" --owner-uid 1001",
        "  use owner UID 1002 and that fork's repository ID only in the separate untrusted runner pool",
        "  a configured CI_WARM_DIR takes precedence; verify its owner, privacy and write access for its pool",
        "  hosted CI without that override uses a run-scoped volume and the portable rust-cache action",
        "  plan/prune on the helper is the owned-path maintenance route; run it only for an idle pool",
    ]


def warm(run, say) -> list:
    """Makes the directories, installs sccache and checks it runs. Returns the problems, and says only what changed."""
    problems = []
    for path in warm_directories():
        if path and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            say(f"made {path}")
    installer = os.path.join(config.REPO, "scripts", "install-sccache.sh")
    if not os.path.exists(installer):
        return [f"{installer} is not in this checkout; fetch upstream main"]
    done, out = run(("sh", installer, os.path.join(config.HOME, "bin")))
    found = out.strip().splitlines()[-1] if out.strip() else ""
    if done != 0 or not found:
        problems.append("sccache could not be installed (no network?); batches will compile without a cache")
    elif run((found, "--version"))[0] != 0:
        problems.append(f"{found} does not run")
    return problems


def setup(home: str, run=run_tool, say=print) -> int:
    """Returns 0 when everything nzfleet needs is in place, 1 when something is missing."""
    say(selfupdate.RULE)
    env_path = os.environ.get("NZFLEET_ENV", os.path.join(home, ".config", "nzfleet", "env"))
    template = _read(os.path.join(BIN, "env.template"))
    repo = config.REPO
    code = config.FILE.get("NZFLEET_CODE") or os.path.join(os.path.dirname(repo), "nzfleet-main", "tools", "nzfleet")
    values = {"HOME": os.path.join(home, ".nzfleet"), "REPO": repo, "WORK": config.WORK, "CODE": code}
    if os.path.exists(env_path):
        absent = missing_variables(template, _read(env_path))
        say(f"env file {env_path} kept" + (f"; it does not mention {', '.join(absent)}" if absent else ""))
    else:
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write(render_env(template, values))
        say(f"wrote {env_path}; open it and fill in what is commented out")

    root = os.path.dirname(os.path.dirname(code))
    if not os.path.isdir(root):
        done, out = run(("git", "-C", repo, "worktree", "add", "--detach", root, "upstream/main"))
        say(f"made the daemon's worktree {root}" if done == 0 else f"could not make {root}: {out.strip()[:200]}")
    wanted = os.path.join(root, "tools", "nzfleet", "bin") if os.path.isdir(root) else BIN
    for name in LINKED:
        problem = _link(os.path.join(wanted, name), name, os.path.join(home, ".local", "bin"))
        if problem:
            say(problem)

    problem = _link(os.path.join(os.path.dirname(wanted), "skill"), "nzfleet", os.path.join(home, ".claude", "skills"))
    if problem:
        say(problem)

    changed = units.sync(home, BIN, lambda: run(("systemctl", "--user", "daemon-reload")))
    if changed:
        say(f"installed {', '.join(changed)}")
    for unit in ("nzfleet.service", "nzfleet-serve.service", "nzfleet-watchdog.timer"):
        done, out = run(("systemctl", "--user", "enable", "--now", unit))
        if done != 0:
            say(f"could not enable {unit}: {out.strip()[:200]}")
    say("Machine tidy, including idle build output, runs through nzfleet every 15 minutes.")
    if run(("loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"))[1].strip() != "Linger=yes":
        say("the user's services stop at logout; run once: sudo loginctl enable-linger $USER")

    tool_env = {**os.environ, "PATH": config.FILE.get("PATH", os.environ.get("PATH", ""))}
    lacking = [n for n in needs() if not n.accepts(*run(n.argv, tool_env))]
    for need in lacking:
        say(f"missing: {need.name}: {need.fix}")
    if lacking:
        say(f"{len(lacking)} prerequisite(s) missing")
    for line in address_pool_advice(run(("docker", "info", "--format", "{{json .DefaultAddressPools}}"))[1]):
        say(line)
    for line in partition_advice(run(("systemctl", "show", "user-1001.slice", "-p", "CPUQuotaPerSecUSec"))[1]):
        say(line)
    for line in statuses.setup_advice(config.STATUS_TOKEN_FILE):
        say(line)
    for problem in warm(run, say):
        say(f"missing: {problem}")
        lacking.append(problem)
    say("")
    for line in ci_runner_steps(repo):
        say(line)
    say("")
    say("As root, once; setup never touches root's crontab:")
    say(f"  sudo install -m 755 {os.path.join(repo, 'scripts', 'runner-prune.sh')} {ROOT_PRUNE}")
    say(f"  sudo crontab -e   and add the line   {ROOT_LINE}")
    say(f"  /etc/sudoers.d/nzfleet-prune   {os.environ.get('USER', '<user>')} ALL=(root) NOPASSWD: {ROOT_PRUNE} check")
    return 1 if lacking else 0

