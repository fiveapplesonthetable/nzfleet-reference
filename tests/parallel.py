"""Runs every test module in its own process, side by side, and reports the slowest cases.

    python3 tests/parallel.py [--jobs N] [--slowest N] [module ...]

A module gets a process and a state directory of its own, so nothing one test leaves behind can reach another.
Exit status is 0 only when every module passes.
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES_PER_PROCESS = 6


class Timed(unittest.TextTestResult):
    """A text result that remembers how long each case took."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seconds = {}
        self._started = 0.0

    def startTest(self, test):
        self._started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.seconds[test.id()] = time.monotonic() - self._started
        super().stopTest(test)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def child(module: str, shard: int, shards: int, report: str) -> int:
    """Runs every `shards`-th case of one module, starting at `shard`, and writes the per-case seconds to `report`."""
    loaded = unittest.defaultTestLoader.loadTestsFromName(f"tests.{module}")
    suite = unittest.TestSuite(case for index, case in enumerate(cases(loaded)) if index % shards == shard)
    runner = unittest.TextTestRunner(resultclass=Timed, verbosity=0)
    result = runner.run(suite)
    with open(report, "w") as output:
        json.dump({"cases": result.testsRun, "seconds": result.seconds}, output)
    return 0 if result.wasSuccessful() else 1


def modules(names: list[str]) -> list[str]:
    found = sorted(name[:-3] for name in os.listdir(HERE) if name.startswith("test_") and name.endswith(".py"))
    return [name for name in found if not names or name in names]


def shards_of(module: str) -> int:
    """How many processes a module is split across: one per few cases, so a module of many cases does not set the pace."""
    with open(os.path.join(HERE, module + ".py")) as source:
        count = sum(1 for line in source if line.lstrip().startswith("def test_"))
    return max(1, -(-count // CASES_PER_PROCESS))


def run_shard(module: str, shard: int, shards: int, scratch: str) -> dict:
    home = os.path.join(scratch, f"{module}-{shard}")
    os.makedirs(home)
    report = os.path.join(home, "report.json")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "NZFLEET_ENV": "/dev/null",
           "NZFLEET_HOME": os.path.join(home, "home"), "TMPDIR": home, "PYTHONPATH": ROOT}
    os.makedirs(env["NZFLEET_HOME"])
    started = time.monotonic()
    done = subprocess.run([sys.executable, os.path.join(HERE, "parallel.py"), "--child", module, str(shard), str(shards), report],
                          cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    try:
        with open(report) as source:
            seen = json.load(source)
    except (OSError, ValueError):
        seen = {"cases": 0, "seconds": {}}
    return {"module": module, "ok": done.returncode == 0, "elapsed": elapsed, "cases": seen["cases"],
            "seconds": seen["seconds"], "output": done.stdout + done.stderr}


def print_report(results: list[dict], slowest: int) -> None:
    per_module = {}
    for r in results:
        row = per_module.setdefault(r["module"], [0, 0.0])
        row[0] += r["cases"]
        row[1] += r["elapsed"]
    print("\nmodule                                    cases  process seconds")
    for module, (count, seconds) in sorted(per_module.items(), key=lambda item: -item[1][1]):
        print(f"{module:40} {count:6} {seconds:10.1f}")
    print("\nslowest cases")
    every = sorted(((s, name) for r in results for name, s in r["seconds"].items()), reverse=True)
    for seconds, name in every[:slowest]:
        print(f"{seconds:7.2f}  {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=min(len(os.sched_getaffinity(0)), 32))
    parser.add_argument("--slowest", type=int, default=0)
    parser.add_argument("--child", nargs=4, metavar=("MODULE", "SHARD", "SHARDS", "REPORT"))
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    if args.child:
        module, shard, shards, report = args.child
        return child(module, int(shard), int(shards), report)

    units = [(name, shard, shards_of(name)) for name in modules(args.names) for shard in range(shards_of(name))]
    units.sort(key=lambda unit: -unit[2])
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as scratch, concurrent.futures.ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(lambda unit: run_shard(*unit, scratch), units))
    failed = [r for r in results if not r["ok"]]
    for r in failed:
        print(f"--- {r['module']} failed\n{r['output']}")
    cases = sum(r["cases"] for r in results)
    print(f"{cases} cases in {len(results)} processes, {len(failed)} failed, {time.monotonic() - started:.1f}s")
    if args.slowest:
        print_report(results, args.slowest)
    return 1 if failed or cases == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
