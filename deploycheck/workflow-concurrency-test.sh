#!/bin/bash
set -euo pipefail

python3 - <<'PYTHON'
from pathlib import Path
import yaml

ci = yaml.load(Path(".github/workflows/ci.yaml").read_text(), Loader=yaml.BaseLoader)
deploy = yaml.load(Path(".github/workflows/deploy.yaml").read_text(), Loader=yaml.BaseLoader)
ci_source = Path(".github/workflows/ci.yaml").read_text()

assert ci["concurrency"]["group"] == "ci-${{ github.ref }}${{ github.event_name == 'workflow_run' && '-full' || '' }}", "the postsubmit full run must not share the push group"
assert ci["concurrency"]["cancel-in-progress"] == "true", "a newer push supersedes main's older runs; only the newest deploys"
assert ci["jobs"]["select"]["outputs"]["deploy"] == "${{ steps.areas.outputs.deploy }}"
assert ci["jobs"]["select"]["outputs"]["base"] == "${{ steps.areas.outputs.base }}"
# The credential scan and the image reuse read no diff, so they need no base fetch.
for name, job in ci["jobs"].items():
    if name in ("select", "journeys-test"):
        continue
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    assert "select" in needs, f"{name} must depend on select"
    if name == "secrets":
        assert "NZELA_BASE" not in job.get("env", {})
        assert not any(step.get("name") == "Fetch selected base" for step in job["steps"])
        continue
    if name in ("reuse-image", "proof"):
        assert "NZELA_BASE" not in job.get("env", {})
        continue
    assert job.get("env", {}).get("NZELA_BASE") == "${{ needs.select.outputs.base }}", name
    if "steps" in job:
        assert any(step.get("name") == "Fetch selected base" for step in job["steps"]), name
assert "NZELA_COMPAT_BASE" not in ci_source
assert "base=\"$(git rev-parse --verify 'origin/main^{commit}')\"" in ci_source
assert "CI_TRUSTED_VERIFIERS" not in ci_source and "statuses" not in ci_source, "no login's status is evidence"
assert 'nzfleet-select proof-select' in ci_source, "a proof is read from a pull request run of this workflow"
assert "pull_request" in ci["on"], "the proof is written by a pull request run"
assert "github.event_name != 'pull_request'" in ci["jobs"]["android-device"]["if"], "the device lane never runs on a pull request"
assert "android" not in ci["jobs"]["proof"]["steps"][1]["run"], "android is not recorded as proved"
assert ci["jobs"]["proof"]["if"].count("github.event_name == 'pull_request'") == 1
for name in ("select", "journeys-test"):
    assert "CI_PR_RUNS_ON" not in ci["jobs"][name].get("if", ""), f"{name} must start on a fork's pull request, which sees no variables"
assert "CI_PR_RUNS_ON" not in ci_source, "a fork's pull request sees no variables"
for name, job in ci["jobs"].items():
    for step in job.get("steps", []):
        assert "secrets." not in str(step), f"{name} reads a secret a pull request run could reach"
post = yaml.load(Path(".github/workflows/postsubmit.yaml").read_text(), Loader=yaml.BaseLoader)
assert post["on"]["workflow_run"]["workflows"] == [deploy["name"]], "postsubmit starts when the deploy has finished, never alongside it"
assert post["on"]["workflow_run"]["workflows"] != ci["name"], "postsubmit must not start on ci completing: it would take the runners the deploy waits for"
assert post["on"]["workflow_run"]["types"] == ["completed"] and post["on"]["workflow_run"]["branches"] == ["main"]
assert post["concurrency"] == {"group": "postsubmit-main", "cancel-in-progress": "true"}, "a newer main supersedes an older full run"
assert "conclusion" not in post["jobs"]["decide"]["if"], "a skipped or failed deploy still gets its full run"
assert "status=success" in post["jobs"]["decide"]["steps"][0]["run"] and "event=push" in post["jobs"]["decide"]["steps"][0]["run"], "decide finds main's green ci run for the deployed commit"
assert "workflow_call" in ci["on"] and "workflow_dispatch" not in ci["on"], "a dispatch could pick the runner from a branch"
assert post["jobs"]["full"]["uses"] == "./.github/workflows/ci.yaml" and post["jobs"]["full"]["with"]["full"] == "true"
assert "workflow_dispatch" not in Path(".github/workflows/deploy.yaml").read_text().split("jobs:")[0], "only a push run may deploy"
assert "&& ! printf '%s\\n' \"$proved\" | grep -qx \"$area\"" in ci_source
assert "console-capture" not in ci["jobs"], "the capture must not be able to cancel the run that gates the deploy"
capture = yaml.load(Path(".github/workflows/console-capture.yaml").read_text(), Loader=yaml.BaseLoader)
for suite in ("console-crawl", "console-capture", "android-crawl", "perf"):
    after = yaml.load(Path(f".github/workflows/{suite}.yaml").read_text(), Loader=yaml.BaseLoader)
    assert after["on"]["workflow_run"]["workflows"] == [deploy["name"]], f"{suite} would take the runners the deploy waits for"
    assert after["on"]["workflow_run"]["branches"] == ["main"]
    assert "contains(fromJSON('[\"success\", \"failure\"]'), github.event.workflow_run.conclusion)" in [job.get("if", "") for job in after["jobs"].values()][0], f"{suite} runs only when ci was green, which a skipped deploy means it was not"
assert "workflow_dispatch" not in capture["on"], "a dispatch would need GitHub machines, which cannot hold the twelve-CPU cap"
capture_if = capture["jobs"]["capture"]["if"]
assert "!github.event.repository.fork" in capture_if
assert deploy["on"]["workflow_run"]["workflows"] == ["CI checks"]
assert deploy["on"]["workflow_run"]["types"] == ["completed"]
assert deploy["jobs"]["gate"]["if"].strip().startswith("!github.event.repository.fork")
assert "github.event.workflow_run.conclusion == 'success'" in deploy["jobs"]["gate"]["if"]
assert "concurrency" not in deploy, "a workflow-level group is taken before gate decides"
assert "if" in deploy["jobs"]["gate"]
for job in ("deploy", "console", "site"):
    assert deploy["jobs"][job]["concurrency"] == {
        "group": f"deploy-test-{job}",
        "cancel-in-progress": "false",
    }

FALLBACK = "{0}-docker.pkg.dev/{1}/nzela"

def evaluate(expression, variables):
    """Evaluate the `a || format(...)` expressions the workflows use."""
    body = expression.strip()[3:-2].strip()
    left, right = body.split(" || ", 1)
    first = variables.get(left.removeprefix("vars."), "")
    if first:
        return first
    fmt, region, project = right[len("format("):-1].split(", ")
    return fmt.strip("'").format(variables.get(region.removeprefix("vars."), ""), variables.get(project.removeprefix("vars."), ""))

def host(registry):
    return registry.split("/", 1)[0]

unset = {"GCP_REGION": "europe-west1", "GCP_PROJECT": "proj"}
custom = {**unset, "REGISTRY": "registry.example/team"}
envs = [deploy["jobs"][job]["env"] for job in ("deploy", "console", "site")] + [ci["jobs"]["image"]["env"]]
for env in envs:
    assert host(evaluate(env["REGISTRY"], unset)) == "europe-west1-docker.pkg.dev"
    assert host(evaluate(env["REGISTRY"], custom)) == "registry.example"
for job in ("deploy", "console", "site"):
    env = deploy["jobs"][job]["env"]
    image = evaluate(env["IMAGE"].split("}}/")[0] + "}}", unset)
    assert image == evaluate(env["REGISTRY"], unset)
api_steps = deploy["jobs"]["deploy"]["steps"]
assert any("gcloud auth configure-docker" in step.get("run", "") for step in api_steps)
for job in ("deploy", "console", "site"):
    for step in deploy["jobs"][job]["steps"]:
        run = step.get("run", "")
        assert 'echo "DEPLOYED=$(' not in run
# The per-job groups queue a run behind an older one, so the order `order`
# checked at the start is stale by the time a component records its commit.
for job, component in (("console", "nzela-console"), ("site", "nzela-site")):
    runs = [step.get("run", "") for step in deploy["jobs"][job]["steps"]]
    record = next(i for i, run in enumerate(runs) if f"release.sh record service {component}" in run)
    assert any(f'release.sh ordered "$SHA" {component}' in run for run in runs[:record]), f"{job} must re-check order before it records"
    checkout = next(step for step in deploy["jobs"][job]["steps"] if step.get("uses", "").startswith("actions/checkout"))
    assert "fetch-depth" not in checkout.get("with", {}), f"{job} checks out one commit; release.sh deepens it for the ancestry check"
assert "${{ github.token }}" in Path(".github/workflows/actions-tidy.yaml").read_text()
print("deploy jobs serialize per job, use REGISTRY and surface command failures")
PYTHON
