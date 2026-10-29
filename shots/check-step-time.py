#!/usr/bin/env python3
"""Run one check command and report its measured work."""

import os
import fnmatch
import re
import selectors
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

HANG = 124
CAPS = Path(os.environ.get("STEP_CAPS", Path(__file__).resolve().parents[3] / "scripts" / "suite-caps"))
NEXTEST_SLOW = re.compile(r"SLOW \[[^]]*\]\s+\(\S+\)\s+(?P<name>.+)$")
NEXTEST_ENDED = re.compile(
    r"^\s*(?:PASS|FAIL|TIMEOUT|SIG[A-Z]+|EXECFAIL|LEAK-FAIL|FLAKY \d+/\d+)\s+\[[^]]*\]\s+\(\S+\)\s+(?P<name>.+)$"
)
GRADLE_STARTED = re.compile(r"^(?P<name>\S.* > .+) STARTED$")
GRADLE_ENDED = re.compile(r"^(?P<name>\S.* > .+) (?:PASSED|FAILED|SKIPPED)$")
PLAYWRIGHT_STARTED = re.compile(r"^\[\d+/\d+\]\s+(?P<name>.+)$")
RING_LINES = 80
RING_BYTES = 32_768
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Measurements:
    def __init__(self) -> None:
        self.tests: int | None = None
        self.files: int | None = None
        self.cache_hits = 0
        self.cache_up_to_date = 0
        self.ring: deque[str] = deque()
        self.ring_bytes = 0
        self.pending = {"stdout": b"", "stderr": b""}
        self.slow: dict[str, None] = {}
        self.last_started: str | None = None

    def feed(self, stream: str, chunk: bytes) -> None:
        output = sys.stdout if stream == "stdout" else sys.stderr
        output.buffer.write(chunk)
        output.buffer.flush()
        data = self.pending[stream] + chunk
        lines = data.split(b"\n")
        self.pending[stream] = lines.pop()
        for raw in lines:
            self.line(raw.decode(errors="replace"))
        while len(self.pending[stream]) > 8192:
            self.line(self.pending[stream][:8192].decode(errors="replace"))
            self.pending[stream] = self.pending[stream][8192:]

    def line(self, line: str) -> None:
        encoded = len(line.encode(errors="replace"))
        self.ring.append(line)
        self.ring_bytes += encoded
        while len(self.ring) > RING_LINES or self.ring_bytes > RING_BYTES:
            self.ring_bytes -= len(self.ring.popleft().encode(errors="replace"))
        parsed = ANSI_ESCAPE.sub("", line)
        if ended := NEXTEST_ENDED.search(parsed) or GRADLE_ENDED.search(parsed):
            self.slow.pop(ended.group("name"), None)
        elif slow := NEXTEST_SLOW.search(parsed) or GRADLE_STARTED.search(parsed):
            self.slow[slow.group("name")] = None
        elif started := PLAYWRIGHT_STARTED.search(parsed):
            self.last_started = started.group("name")
        file_match = re.search(r"Test Files\s+(\d+)", parsed)
        if file_match:
            self.files = int(file_match.group(1))
        rust = re.search(r"test result: (?:ok|FAILED)\.\s*(\d+) passed", parsed)
        vitest = re.search(r"^\s*Tests\s+(\d+) passed\b", parsed)
        playwright = re.search(r"^\s*(\d+) passed \([0-9.]+[smh]+\)$", parsed)
        if rust:
            self.tests = (self.tests or 0) + int(rust.group(1))
        elif vitest:
            self.tests = int(vitest.group(1))
        elif playwright:
            self.tests = int(playwright.group(1))
        self.cache_hits += len(re.findall(r"\bFROM-CACHE\b", parsed))
        self.cache_up_to_date += len(re.findall(r"\bUP-TO-DATE\b", parsed))

    def summary(self) -> tuple[str, str]:
        tests = str(self.tests) if self.tests is not None else "unobserved"
        files = f" files={self.files}" if self.files is not None else ""
        cache = (
            f"cache_hits={self.cache_hits} cache_up_to_date={self.cache_up_to_date}"
            if self.cache_hits or self.cache_up_to_date
            else "cache=unobserved"
        )
        return f"tests={tests}{files}", cache


def cap_for(name: str) -> int | None:
    """Seconds the step called `name` may run: the first line of suite-caps whose pattern matches."""
    for row in CAPS.read_text().splitlines():
        fields = row.split()
        if len(fields) == 2 and fnmatch.fnmatchcase(name, fields[0]):
            return int(fields[1])
    return None


def remove_container(name: str | None) -> None:
    """A SIGKILL to the step runs no trap, and docker does not stop a container whose client died."""
    if name:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def hang_report(name: str, cap: int, metrics: Measurements) -> list[str]:
    lines = [f"HANG: {name} was still running after its cap of {cap}s and was killed."]
    if metrics.slow:
        lines += ["running when it was killed:", *(f"  {test}" for test in metrics.slow)]
    if metrics.last_started:
        lines.append(f"last test the runner started: {metrics.last_started}")
    lines.append(f"last {len(metrics.ring)} lines of output:")
    return lines + [f"  | {line}" for line in metrics.ring]


def xml_snapshot(directory: str | None) -> dict[Path, tuple[int, int, int, bytes]]:
    if not directory:
        return {}
    snapshot: dict[Path, tuple[int, int, int, bytes]] = {}
    for report in Path(directory).glob("*.xml"):
        try:
            stat = report.stat()
            snapshot[report] = (stat.st_ino, stat.st_size, stat.st_mtime_ns, report.read_bytes())
        except OSError:
            continue
    return snapshot


def xml_test_count(
    directory: str | None, before: dict[Path, tuple[int, int, int, bytes]],
) -> int | None:
    if not directory:
        return None
    total = 0
    found = False
    for report in Path(directory).glob("*.xml"):
        try:
            stat = report.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns, report.read_bytes())
            if before.get(report) == identity:
                continue
            count = ET.parse(report).getroot().attrib.get("tests")
            if count and count.isdigit():
                total += int(count)
                found = True
        except (ET.ParseError, OSError):
            continue
    return total if found else None


def main() -> int:
    arguments = sys.argv[1:]
    test_results = None
    if len(arguments) >= 2 and arguments[0] == "--test-results":
        test_results = arguments[1]
        arguments = arguments[2:]
    if len(arguments) < 3 or arguments[1] != "--":
        print("usage: check-step-time.py [--test-results <dir>] <name> -- <command> [args...]", file=sys.stderr)
        return 2
    name = arguments[0]
    cap = cap_for(name)
    if cap is None:
        print(f"check-step: {name} has no cap in scripts/suite-caps", file=sys.stderr)
        return 2
    before_xml = xml_snapshot(test_results)
    started_wall = time.monotonic()
    started_cpu = os.times()
    metrics = Measurements()
    try:
        child = subprocess.Popen(
            arguments[2:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
    except OSError as error:
        print(f"check-step: could not start command: {error}", file=sys.stderr)
        status = 127
    else:
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        hung = False
        while selector.get_map():
            ready = selector.select(timeout=max(0.0, started_wall + cap - time.monotonic()))
            if not ready:
                hung = True
                os.killpg(child.pid, signal.SIGKILL)
                remove_container(os.environ.get("SHOT_CONTAINER_NAME"))
                break
            for key, _ in ready:
                chunk = os.read(key.fd, 8192)
                if chunk:
                    metrics.feed(key.data, chunk)
                else:
                    selector.unregister(key.fileobj)
        status = child.wait()
        if status < 0:
            status = 128 - status
        if hung:
            status = HANG
            print("\n".join(hang_report(name, cap, metrics)), file=sys.stderr)
        for stream, pending in metrics.pending.items():
            if pending:
                metrics.line(pending.decode(errors="replace"))
    elapsed = time.monotonic() - started_wall
    finished_cpu = os.times()
    xml_count = xml_test_count(test_results, before_xml)
    if xml_count is not None:
        metrics.tests = xml_count
    tests, cache = metrics.summary()
    child_user = finished_cpu.children_user - started_cpu.children_user
    child_system = finished_cpu.children_system - started_cpu.children_system
    print(
        f"check-step: {name} wall={elapsed:.3f}s cpu={child_user + child_system:.3f}s "
        f"exit={status} {tests} {cache}", file=sys.stderr,
    )
    if status != 0:
        for line in metrics.ring:
            print(f"check-step output: {line}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
