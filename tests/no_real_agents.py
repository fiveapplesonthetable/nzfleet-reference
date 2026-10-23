"""Makes any agent or outward-facing command (`claude`, `codex`, `gh`, `docker`) that reaches Popen raise, so a test that would launch one fails where it does."""

import os
import subprocess

_real = subprocess.Popen
REFUSED = {"claude", "codex", "gh", "docker"}


class RealAgentLaunched(BaseException):
    """Not an Exception, so the daemon code under test cannot catch it and carry on."""


class RefusesClaude(_real):
    def __init__(self, args, *rest, **kw):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else args
        if any(os.path.basename(os.fsdecode(part)) in REFUSED for part in argv):
            raise RealAgentLaunched(f"a test launched a real agent or tool: {list(argv)[:3]}")
        super().__init__(args, *rest, **kw)


if subprocess.Popen is _real:
    subprocess.Popen = RefusesClaude
