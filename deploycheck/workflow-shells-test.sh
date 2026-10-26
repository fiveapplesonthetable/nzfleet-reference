#!/bin/bash
#
# Check that every run: block in workflows uses shell syntax valid for its
# execution shell. A bash-only here-string in a job with a container will fail
# at runtime when sh runs it.

set -uo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$here/../../.." || exit 1
tools/nzfleet/bin/nzfleet-select tracked workflow-shells '.github/workflows/*.yaml' '.github/workflows/*.yml' >/dev/null || exit 1

# Parse workflows with Python (PyYAML available)
parse_workflows() {
    local workflows_dir="$1"

    python3 - "$workflows_dir" <<'PYTHON'
import sys, yaml, os, re, subprocess

def get_shell_for_step(job, step):
    """Determine the shell for a step."""
    # 1. Explicit shell on the step
    if isinstance(step, dict) and "shell" in step:
        shell_spec = step["shell"]
        if isinstance(shell_spec, str):
            return shell_spec.split()[0]  # e.g., "bash {0}" -> "bash"

    # 2. Job defaults
    if isinstance(job, dict) and "defaults" in job:
        defaults = job["defaults"]
        if isinstance(defaults, dict) and "run" in defaults:
            run_defaults = defaults["run"]
            if isinstance(run_defaults, dict) and "shell" in run_defaults:
                shell_spec = run_defaults["shell"]
                if isinstance(shell_spec, str):
                    return shell_spec.split()[0]

    # 3. Container implies sh, otherwise bash
    if isinstance(job, dict) and "container" in job:
        return "sh"
    return "bash"

def sanitize_block(block):
    """Replace GitHub expressions with a placeholder."""
    return re.sub(r'\$\{\{[^}]*\}\}', 'EXPR', block)

def check_syntax(block, shell):
    """Check shell syntax of a block."""
    if shell not in ("sh", "bash"):
        return True, ""  # Skip unknown shells

    cmd = "dash" if shell == "sh" else "bash"
    result = subprocess.run(
        [cmd, "-n"],
        input=block.encode(),
        capture_output=True
    )
    return result.returncode == 0, result.stderr.decode() if result.stderr else ""

# dash parses these without complaint and fails only when the step runs.
BASH_ONLY = (
    (re.compile(r"(?m)^\s*(?:if|elif|while|until|&&|\|\||!)?\s*\[\["), "[["),
    (re.compile(r"(?m)(?:^|[;&|(]\s*)source\s"), "source"),
    (re.compile(r"\bpipefail\b"), "set -o pipefail"),
    (re.compile(r"<\("), "process substitution"),
    (re.compile(r"\$'"), "$'...' quoting"),
    (re.compile(r"\$\{[A-Za-z_0-9]+(?://|\^\^|,,|:[0-9$(][^}]*:)"), "bash parameter expansion"),
    (re.compile(r"(?m)^\s*function\s+\w+"), "function keyword"),
    (re.compile(r"\w=\("), "array assignment"),
)

SCRIPT_WORD = re.compile(r"(?<![\w$./~-])((?:\.{1,2}/)*(?:scripts|tools|frontend|docs|\.github)/[\w./-]+\.(?:sh|py|mjs|js))(?![\w/-])")

def bash_only(block):
    return [name for pattern, name in BASH_ONLY if pattern.search(block)]

def working_directory(job, step):
    """The step's directory relative to the repository root, or None when an expression decides it."""
    default = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", "")
    chosen = step.get("working-directory", default)
    return None if "${{" in str(chosen) else str(chosen)

def unresolved_scripts(block, directory):
    """Script paths in a block that exist from the repository root but not from the step's directory."""
    if directory is None or re.search(r"(?m)(?:^|[;&|]\s*)cd\s", block):
        return []
    found = []
    for word in SCRIPT_WORD.findall(block):
        if "$" in word or os.path.exists(os.path.join(directory, word)):
            continue
        found.append(word)
    return found

workflows_dir = sys.argv[1]
bad = 0
checked = 0

for filename in sorted(os.listdir(workflows_dir)):
    if not (filename.endswith(".yaml") or filename.endswith(".yml")):
        continue

    path = os.path.join(workflows_dir, filename)
    try:
        with open(path) as f:
            workflow = yaml.safe_load(f)
    except Exception as e:
        print(f"{path}: cannot parse workflow: {e}", file=sys.stderr)
        bad += 1
        continue

    if not workflow or not isinstance(workflow, dict) or "jobs" not in workflow:
        print(f"{path}: workflow has no jobs mapping", file=sys.stderr)
        bad += 1
        continue

    jobs = workflow["jobs"]
    if not isinstance(jobs, dict):
        print(f"{path}: jobs is not a mapping", file=sys.stderr)
        bad += 1
        continue

    for job_name, job in jobs.items():
        if not job or not isinstance(job, dict) or "steps" not in job:
            continue

        steps = job["steps"]
        if not isinstance(steps, list):
            continue

        for step_idx, step in enumerate(steps):
            if not step or not isinstance(step, dict) or "run" not in step:
                continue

            block = step["run"]
            if not isinstance(block, str):
                continue

            shell = get_shell_for_step(job, step)
            sanitized = sanitize_block(block)

            checked += shell in ("sh", "bash")
            step_name = step.get("name", step.get("id", "unnamed"))
            where = f"{path}: job '{job_name}', step {step_idx + 1} '{step_name}'"
            ok, error = check_syntax(sanitized, shell)
            if not ok:
                print(f"{where}: {shell} syntax failed", file=sys.stderr)
                print(error.strip() or "shell returned failure without a diagnostic", file=sys.stderr)
                bad += 1
            if shell == "sh":
                for name in bash_only(sanitized):
                    print(f"{where}: sh step uses {name}, which only bash has", file=sys.stderr)
                    bad += 1
            for script in unresolved_scripts(sanitized, working_directory(job, step)):
                print(f"{where}: {script} does not exist from the step's working directory", file=sys.stderr)
                bad += 1

if not bad:
    print(f"workflow-shells: {checked} sh/bash run blocks passed")
sys.exit(min(bad, 1))  # Exit 0 or 1, not a count
PYTHON
}

main() {
    local workflows_dir="${1:-.github/workflows}"

    if [ ! -d "$workflows_dir" ]; then
        printf 'workflow-shells: no workflows directory: %s\n' "$workflows_dir" >&2
        return 1
    fi

    parse_workflows "$workflows_dir"
}

main "$@"
