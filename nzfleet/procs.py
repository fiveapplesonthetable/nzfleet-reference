"""Processes an agent starts, waits on and stops. Each is named, lives under the worktree, and is the only kind it may kill."""

from __future__ import annotations

import os
import signal
import subprocess
import time

from . import config, plan, world

WAIT_LIMIT = 9 * 60  # under the ten minutes a tool call may last
BEAT = 60


def root() -> str:
    """The worktree the command is run in, or the current directory when it is not in one."""
    try:
        return world.git("rev-parse", "--show-toplevel", cwd=os.getcwd()).strip()
    except (world.Failed, OSError):
        return os.getcwd()


def _file(wt: str, name: str, ext: str) -> str:
    path = os.path.join(wt, ".nzrun")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, f"{name}.{ext}")


def load_env(wt: str) -> bool:
    """Reads the worktree's .nzenv into this process, so a command works even when the shell never sourced it."""
    try:
        with open(os.path.join(wt, ".nzenv")) as f:
            values = plan.parse_env(f.read())
    except OSError:
        return False
    os.environ.update(values)
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def group_alive(group: int, proc: str = "/proc") -> bool:
    """Whether a process group still has a non-zombie member."""
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat")) as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                if fields[0] != "Z" and int(fields[2]) == group:
                    return True
            except (OSError, IndexError, ValueError):
                continue
    return False


def _pid(wt: str, name: str):
    try:
        with open(_file(wt, name, "pid")) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def wait_for(is_alive, say, clock=time.monotonic, sleep=time.sleep, limit: float = WAIT_LIMIT, beat: float = BEAT) -> int:
    """Waits until `is_alive` is false (returns 0) or `limit` seconds pass (returns 3), calling `say(elapsed)` every `beat`."""
    began = clock()
    last = began
    while is_alive():
        now = clock()
        if now - began >= limit:
            return 3
        if now - last >= beat:
            say(now - began)
            last = now
        sleep(1.0)
    return 0


def last_line(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1][:160] if lines else ""


def start(name: str, argv: list, wt: str, env=None, cwd=None) -> str:
    """Starts `argv` detached in `wt`, with a pidfile, a log, and an exit file written when it ends. Returns why not, or ''."""
    if not plan.NAME.match(name):
        return f"a name is lowercase letters, digits, dots, dashes and underscores, up to 40: {name!r}"
    old = _pid(wt, name)
    if old and alive(old):
        return f"{name} is already running (pid {old}). Wait: nzfleet wait {name}   Stop it: nzfleet kill {name}"
    for ext in ("exit", "pid"):
        try:
            os.remove(_file(wt, name, ext))
        except OSError:
            pass
    wrapper = f'"$@"; echo $? > {_file(wt, name, "exit")}'
    with open(_file(wt, name, "log"), "w") as log:
        proc = subprocess.Popen(["bash", "-c", wrapper, "nz", *argv], cwd=cwd or os.getcwd(), env=config.with_private_tmpdir(env), stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(_file(wt, name, "pid"), "w") as f:
        f.write(str(proc.pid))
    return ""


def wait(target: str, wt: str, out=print) -> int:
    """0 when the process ended, 3 when it is still running, 1 when there is no such process."""
    if target.isdigit():
        pid, log, exit_file = int(target), "", "/nonexistent"
    else:
        pid = _pid(wt, target)
        if pid is None:
            out(f"nothing named {target} was started here. Start it with: nzfleet run {target} -- <command>")
            return 1
        log, exit_file = _file(wt, target, "log"), _file(wt, target, "exit")
    def say(t: float) -> None:
        out(f"{target} still running after {int(t // 60)} min" + (f"; last line: {last_line(log)}" if log else ""))

    rc = wait_for(lambda: alive(pid) and not os.path.exists(exit_file), say, limit=WAIT_LIMIT, beat=BEAT)
    if rc == 3:
        out(f"{target} is still running. Call again: nzfleet wait {target}")
        return 3
    if not log:
        out(f"process {pid} ended")
        return 0
    try:
        code = open(exit_file).read().strip()
    except OSError:
        code = "unknown (it was killed)"
    out(f"{target} ended with exit code {code}" + ("" if code == "0" else "  <- it FAILED"))
    try:
        out("--- last lines of the log\n" + plan.tail(open(log, errors="replace").read(), 25))
    except OSError:
        pass
    return 0


def kill(name: str, wt: str, now: bool = False) -> str:
    """Stops the named process and everything it started. Returns why not, or ''."""
    pid = _pid(wt, name)
    if pid is None:
        return f"nothing named {name} was started here"
    if not alive(pid):
        return ""
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        group = os.getpgid(pid)
    except OSError:
        return ""
    if not plan.owns(cwd, wt) or group != pid:
        return f"pid {pid} is not a process this worktree started (it runs in {cwd}); not touching it"
    kill_group(pid, now=now)
    if wait_for(lambda: group_alive(pid), lambda t: print(f"{name} (pid {pid}) is still stopping after {int(t // 60)} min")) == 3:
        return f"pid {pid} was sent SIGTERM and is still running; if it ignores it: nzfleet kill {name} --now"
    return ""


def kill_group(pid: int, now: bool = False) -> bool:
    """Signals a process group's leader and everything it started: SIGTERM, or SIGKILL with `now`. Returns whether the
    leader has exited, without waiting for it, so a leader that ignores SIGTERM cannot stall the caller."""
    try:
        os.killpg(pid, signal.SIGKILL if now else signal.SIGTERM)
    except OSError:
        return True
    return not alive(pid)
