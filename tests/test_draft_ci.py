import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import yaml

from nzfleet import plan, state, tick, world

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yaml")

# Jobs a draft pull request may schedule: selection, the credential scan, and static checks.
CHEAP = {"journeys-test", "select", "secrets", "console", "site", "deploy", "fleet", "contract", "check"}
# Of those, `check` may only run its lint lane.
LINT_ONLY = {"check"}

TOKEN = re.compile(r"'[^']*'|\|\||&&|!=|==|!|\(|\)|,|[\w.*]+")
FIXED = {
    "github.event_name": "'pull_request'",
    "github.event.repository.fork": "False",
    "github.ref": "'refs/pull/1/merge'",
    "inputs.full": "False",
    "needs.select.result": "'success'",
    "true": "True",
    "false": "False",
}


def evaluate(expression: str, draft: bool):
    """A job's `if` or matrix expression, read as the pull request event with every area selected."""
    out = []
    for token in TOKEN.findall(expression.strip().removeprefix("${{").removesuffix("}}")):
        if token == "&&":
            out.append(" and ")
        elif token == "||":
            out.append(" or ")
        elif token == "!":
            out.append(" not ")
        elif token == "github.event.pull_request.draft":
            out.append(str(draft))
        elif token.startswith("needs.select.outputs."):
            out.append("'true'")
        elif token in ("contains", "fromJSON", "always", "cancelled"):
            out.append({"contains": "_contains", "fromJSON": "_json", "always": "_always", "cancelled": "_cancelled"}[token])
        elif token == "needs.*.result":
            out.append("None")
        elif token.startswith("vars."):
            out.append("''")
        elif token in FIXED:
            out.append(FIXED[token])
        else:
            out.append(token)
    scope = {"_contains": lambda *_: False, "_json": json.loads, "_always": lambda: True, "_cancelled": lambda: False}
    return eval("".join(out), {"__builtins__": {}}, scope)


def scheduled(draft: bool) -> dict:
    jobs = yaml.load(open(WORKFLOW).read(), Loader=yaml.BaseLoader)["jobs"]
    found = {}
    for name, job in jobs.items():
        if "if" in job and not evaluate(job["if"], draft):
            continue
        matrix = job.get("strategy", {}).get("matrix")
        lanes = evaluate(matrix, draft)["include"] if isinstance(matrix, str) else []
        found[name] = [m["lane"] for m in lanes]
    return found


class DraftPullRequestCi(unittest.TestCase):
    def test_a_draft_schedules_only_the_cheap_jobs(self):
        self.assertEqual(set(scheduled(True)) - CHEAP, set())

    def test_a_draft_runs_only_the_lint_lane_of_the_rust_checks(self):
        self.assertEqual(scheduled(True)["check"], ["lint"])

    def test_a_draft_does_not_record_a_proof(self):
        self.assertNotIn("proof", scheduled(True))

    def test_a_ready_pull_request_schedules_every_heavy_job_and_the_tests(self):
        ready = scheduled(False)
        for job in ("compat", "e2e", "console-browser", "android", "image", "proof", "rust-tests"):
            self.assertIn(job, ready)

    def test_marking_a_pull_request_ready_starts_its_run(self):
        on = yaml.load(open(WORKFLOW).read(), Loader=yaml.BaseLoader)["on"]
        self.assertIn("ready_for_review", on["pull_request"]["types"])
        self.assertIn("synchronize", on["pull_request"]["types"])



class ReadyForReview(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        state.PATH = os.path.join(home, "state.json")
        state.LOCK = os.path.join(home, "state.lock")
        self.addCleanup(shutil.rmtree, home, True)

    def ready(self, numbers):
        with state.change() as s:
            for n in numbers:
                s["batches"][f"b{n}"] = {"areas": ["deploy"], "started": n}
                s["passed"][str(n)] = {"sha": f"s{n}", "main": "m", "order": [n], "batch": f"b{n}"}
        prs = [plan.Pr(n, f"s{n}", frozenset({"deploy"}), draft=True) for n in numbers]
        with mock.patch.object(world, "merged_since", return_value=[]), \
                mock.patch.object(world, "merges_cleanly", return_value=[]), \
                mock.patch.object(world, "mark_ready") as mark_ready:
            tick.ready(prs, dry_run=False)
        return [c.args[0] for c in mark_ready.call_args_list]

    def test_a_pull_request_sent_alone_is_marked_ready(self):
        self.assertEqual(self.ready([921]), [921])

    def test_a_member_of_a_train_stays_a_draft(self):
        self.assertEqual(self.ready([921, 922]), [])
