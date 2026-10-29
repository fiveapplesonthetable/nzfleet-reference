"""Puts the module-level settings of nzfleet.config and the process environment back after a test, and keeps the live disks out of machine.busy()."""

import os
from unittest import mock

from nzfleet import config, machine

SETTINGS = ("AGENT_SLOTS", "MAX_CPU_PERCENT", "CONFIG_PROBLEM", "_ENVIRONMENT")


def isolate(test):
    environment = mock.patch.dict(os.environ)
    environment.start()
    test.addCleanup(environment.stop)
    for name in SETTINGS:
        patch = mock.patch.object(config, name, getattr(config, name))
        patch.start()
        test.addCleanup(patch.stop)
    # busy() reads free space on real paths before it looks at the CPU, so a full runner disk would make every CPU test return True.
    disk = mock.patch.object(machine, "free_gb", return_value=1000)
    disk.start()
    test.addCleanup(disk.stop)
