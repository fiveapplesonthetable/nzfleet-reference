"""A run step before a job's checkout names its own directory when the job's default is inside the tree."""
import pathlib
import sys

import yaml

bad = []
for path in sorted(pathlib.Path(".github/workflows").glob("*.y*ml")):
    workflow = yaml.safe_load(path.read_text()) or {}
    for name, job in (workflow.get("jobs") or {}).items():
        default = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
        if not default or default.startswith("${{"):
            continue
        for step in job.get("steps") or []:
            if "actions/checkout" in str(step.get("uses", "")):
                break
            if "run" in step and "working-directory" not in step:
                bad.append(f"{path}: job '{name}', step '{step.get('name', step['run'][:40])}' runs in {default} before the checkout makes it")
for line in bad:
    print(line, file=sys.stderr)
sys.exit(1 if bad else 0)
