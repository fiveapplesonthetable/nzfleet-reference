"""Email to the owner, through the machine's notify.py. It only ever writes to the owner's own address."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from .config import NOTIFY

_SEND = "import sys; sys.path.insert(0, sys.argv[1]); import notify; sys.exit(notify.notify({}, subject=sys.argv[2], body=sys.stdin.read()))"


def send(subject: str, body: str) -> bool:
    """True when the mail went out, or when no notifier is configured and so there is nothing to retry."""
    if not NOTIFY:
        logging.warning("NZFLEET_NOTIFY is not configured; owner notification was not sent")
        return True
    done = subprocess.run([sys.executable, "-c", _SEND, os.path.dirname(NOTIFY), subject],
                          input=body, capture_output=True, text=True, timeout=120)
    return done.returncode == 0
