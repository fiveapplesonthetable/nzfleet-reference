"""The checkout nzfleet runs from: git, the self-test, the owner's issue and the re-exec."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from . import config, selfupdate, state, tick, units, world

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_TIMEOUT = 20 * 60


def ci_is_green(sha: str) -> bool:
    jobs = world.ci_jobs(sha)
    return bool(jobs) and jobs[0] == "completed" and bool(jobs[1]) and all(c in ("success", "skipped") for c in jobs[1].values())


def run_own_tests(code: str) -> tuple:
    """(passed, the end of the output). The tests get no env file, a home of their own and no licence to start agents."""
    env = {k: v for k, v in os.environ.items() if k != "NZFLEET_LIVE"}
    with tempfile.TemporaryDirectory(dir=config.private_tmpdir()) as home:
        env.update(NZFLEET_ENV="/dev/null", NZFLEET_HOME=home, TMPDIR=os.path.join(home, "tmp"))
        os.mkdir(env["TMPDIR"], mode=0o700)
        try:
            done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-q", "tests"], cwd=code, env=env,
                                  capture_output=True, text=True, timeout=TESTS_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"could not run: {e}"
    return done.returncode == 0, (done.stdout + done.stderr)[-1500:]


def restart_service() -> None:
    """Has systemd restart the daemon, which is the only way a changed unit's environment reaches it."""
    subprocess.run(["systemctl", "--user", "restart", units.SERVICE], capture_output=True, timeout=60)


def reexec(code: str) -> None:
    """Replaces this process with a daemon on the moved code. Children (agents, batches) are in their own sessions and carry on."""
    os.chdir(code)
    os.execv(sys.executable, [sys.executable, "-m", "nzfleet", "daemon"])


class Checkout:
    def __init__(self, code: str = CODE, green=ci_is_green, test=run_own_tests, say=None, restart=None, sync=None, service=restart_service):
        self.code, self.is_green, self.test = code, green, test
        self.say = say or tick.tell_event
        self.exec = restart or (lambda: reexec(self.code))
        self.sync, self.service, self.unit_changed = sync or (lambda: units.sync(shipped_from=os.path.join(self.code, "bin"))), service, False

    def _git(self, *args) -> str:
        return world.git(*args, cwd=self.code, check=False).strip()

    def detached(self) -> bool:
        return not world.git_ok("symbolic-ref", "-q", "HEAD", cwd=self.code)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def main(self) -> str:
        return self._git("rev-parse", world.MAIN)

    def behind(self) -> tuple:
        lines = self._git("log", "--format=%H %ct", f"HEAD..{world.MAIN}", "--", ":(top)tools/nzfleet").splitlines()
        if lines:
            # Path history can omit a merge whose tool tree matches its PR parent; CI proves the merged main tree.
            main = self._git("show", "-s", "--format=%H %ct", world.MAIN)
            if main and main not in lines:
                lines.insert(0, main)
        return tuple(selfupdate.Behind(sha, float(at)) for sha, at in (line.split() for line in lines))

    def green(self, sha: str) -> bool:
        return self.is_green(sha)

    def refused(self) -> str:
        return state.read()["code_refused"]

    def move(self, sha: str) -> bool:
        return world.git_ok("checkout", "-q", "--detach", sha, cwd=self.code)

    def self_test(self) -> tuple:
        return self.test(self.code)

    def refuse(self, sha: str) -> None:
        with state.change() as s:
            s["code_refused"] = sha

    def moved(self, before: str, after: str) -> None:
        with state.change() as s:
            s["code_moved"] = {"at": time.time(), "from": before, "to": after}
        self.say(f"nzfleet code moved from {before[:8]} to {after[:8]}; restarting on it")

    def tell(self, text: str) -> None:
        self.say(text.splitlines()[0])
        world.comment(config.OWNER_ISSUE, text)

    def install_units(self) -> None:
        changed = self.sync()
        self.unit_changed = units.SERVICE in changed
        if changed:
            self.say(f"nzfleet units reinstalled from {self.code}: {', '.join(changed)}")

    def restart(self) -> None:
        self.service() if self.unit_changed else self.exec()
