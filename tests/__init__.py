import os
import sys

# CPU allocation tests use this runner's 36-CPU batch partition, regardless of the test process affinity.
# A test that runs a real process under taskset takes its CPUs from REAL_AFFINITY instead.
REAL_AFFINITY = os.sched_getaffinity
os.sched_getaffinity = lambda _pid: set(range(36, 72))

# The test modules import each other by bare name, which `discover tests` allows and a plain `unittest` run does not.
sys.path.insert(0, os.path.dirname(__file__))

import no_real_agents  # noqa: E402,F401

import live_home  # noqa: E402
from nzfleet import config  # noqa: E402

live_home.check(config.HOME, config.FILE, os.environ)
