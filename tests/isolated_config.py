"""Puts the module-level settings of nzfleet.config and the process environment back after a test."""

import os
from unittest import mock

from nzfleet import config

SETTINGS = ("AGENT_SLOTS", "MAX_CPU_PERCENT", "CONFIG_PROBLEM", "_ENVIRONMENT")


def isolate(test):
    environment = mock.patch.dict(os.environ)
    environment.start()
    test.addCleanup(environment.stop)
    for name in SETTINGS:
        patch = mock.patch.object(config, name, getattr(config, name))
        patch.start()
        test.addCleanup(patch.stop)
