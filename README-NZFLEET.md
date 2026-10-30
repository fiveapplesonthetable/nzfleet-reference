# nzfleet

nzfleet runs from the checkout named by NZFLEET_CODE, which the daemon moves to main when main's ci is green; `nzfleet version` says what runs and how far behind it is.

Moves nzela pull requests from draft to deployed: it batches them, checks each batch once over main, finds the PR that breaks one, tells the owner what to merge, and reports the test deployment workflow's outcome. A person or an agent drives it with the same commands; `nzfleet tick` does one pass of everything and the daemon runs it every minute.

It merges only through `nzfleet land`, which the owner runs; it never applies Terraform, and never pushes anything but a PR branch replayed onto its own base.

## Commands

| Command | What it does |
|---|---|
| `nzfleet version` | The commit nzfleet runs, main's, how many nzfleet commits it is behind, and the last move. |
| `nzfleet status [--json] [--section S]` | The last tick's snapshot, in under a second: headline, needs you, needs attention (each with its fix command), pipeline, agents, machine (CPU by owner beside its quota, from `NZFLEET_CPU_QUOTAS`), cost, held and stale. `--json` prints the versioned schema, documented in `--help`; `needs`, `progress` and `flakes` are section aliases. |
| `nzfleet tick [--dry-run]` | One pass of the loop below. `--dry-run` prints what it would do and does nothing. |
| `nzfleet watchdog` | Opens, updates or closes the "nzfleet is down" issue by whether the daemon is active and `tick.log` is fresh. Run by `nzfleet-watchdog.timer`. |
| `nzfleet daemon` | `tick` every 60 seconds, forever. Run by `nzfleet.service`. |
| `nzfleet batch <pr>... [--areas a,b] [--foreground]` | Checks those PRs as one batch now. A red one is reported once with its failing areas and not run again or bisected. Detaches, with the machine's env file winning over the calling shell, and prints its log path; the verdict lands in `status`. `--foreground` waits and prints it. |
| `nzfleet replay <pr> [--dry-run]` | Rewrites a PR branch as its own commits on its base, if that applies cleanly. |
| `nzfleet top` | The status sections redrawn every three seconds in plain ANSI, newest events at the bottom, lower sections collapsed to fit; q quits. Without a terminal it prints one frame and exits. |
| `nzfleet serve` | The daemon writes `status.html` from the snapshot each tick; this serves it, read-only, on the Tailscale address and port `NZFLEET_SERVE_PORT` (8787), and refuses to start without a tailnet address. Run by `nzfleet-serve.service`, which `setup` installs. The page refreshes itself and its merge command copies with one tap. |
| `nzfleet events [--since 1h] [-n N] [-f]` | The event log, newest last. It exits; `-f` keeps following. |
| `nzfleet flakes` | The tests that failed and passed on rerun, most often first, with when each last did. |
| `nzfleet queue` | Prints the one-line land command. |
| `nzfleet train [--max N] [pr...]` | Merges the open PRs (default: every unheld one whose task is done) into one pull request: no-frontend PRs first, then small to large, same screens adjacent. Screenshot PNGs that conflict take the later PR's copy; a file in `generators.json` takes it and its command runs on the result; any other conflict leaves the PR out and queues `rebase-<n>` after the train. The result is compiled (clippy, the fuzz lock, `checks.sh compile`); a PR that breaks it is dropped by bisection and gets a `fix-compile-<n>` task. Shots are drawn once on the result and `nzfleet batch` starts on the draft train PR. After its exact final tree passes every required check, a reviewer releases the current head with `nzfleet release-train <pr>`; only then is the PR marked ready and the owner mailed the single `gh pr merge` command. `status` shows each train's review and notice state; an interrupted send is marked uncertain and is not retried automatically to avoid duplicate merge instructions. |
| `nzfleet land <train>` | Merges a verified train in order if main is still where it was checked, then checks main's tree is the verified one. |
| `nzfleet start <slug> [--base main\|<pr>]` | A worktree, branch, ports, database and node_modules of the agent's own. See the agent kit below. |
| `nzfleet prove` | Runs the checks the change calls for, in the foreground. |
| `nzfleet run <name> -- <cmd>`, `wait <name>`, `kill <name>` | A detached command of the agent's own, waited on for at most nine minutes, and stopped by name. |
| `nzfleet pr "<title>" -m "<why>" [--dry-run]` | Pushes the branch and opens or updates its draft PR, once the proof covers what is committed. |
| `nzfleet hold <key\|group> --why "..."`, `nzfleet release <key\|group>` | Stops a queued task, or a batch group (`core`, `android`, `console`, `light`, `wide`), from starting until released. Shown under HELD in `status`. |
| `nzfleet note <key> "<phase>: <line>"` | An agent says which phase it is in (read, write, test, fix, push). Every brief ends by asking for one at each change. |
| `nzfleet progress` (alias of `status --section progress`) | Per agent: time since its last commit, last file change and last test run, commits, changed files, CPU minutes, phase. Per chain: steps in the last hour and day, median time per kind of step, owner wait against machine wait, current head and how long it has been there. |
| `nzfleet starve [--slice N] [--cpu-set 0-3] -- <cmd>` | Runs the command for at most five minutes, with 4 burners per CPU on at most one eighth of fleet CPUs (default the highest 4). It refuses to start during a merge-check batch and stops its burners when one starts. Agents use it instead of free-running `yes` loops; a PreToolUse hook (`bin/burner-guard`, called from the agents' `bash-guard.py`) refuses `yes`, `stress` and `stress-ng` in a Bash command that does not run under it. |
| `nzfleet retire <key> --why "..."` | Takes a held task whose work is done or superseded out of the queue and `status`, and records why. `nzfleet todo --retired` lists them. `next` says "decide: release or retire" for a hold older than 24 hours. |
| `nzfleet setup` | Brings a machine to what nzfleet needs and lists what is still missing. Safe to repeat; see "A fresh machine". |
| `nzfleet tidy [--dry-run]` | The machine tidy pass now, whatever the schedule. |
| `nzfleet tidy --stale-runs DAYS [--dry-run]` | Retires unfinished runs older than `DAYS` only when their process is gone and they have no open PR. |
| `nzfleet db <name> [checkout]` | A migrated Postgres of one's own; `bin/agent-db` calls it. |

## The loop (`tick`)

1. **Read.** Main's head, and every open PR with its head SHA and changed files. GitHub and git are the truth; `state.json` only remembers what this tool decided.
2. **Shape.** A PR whose branch carries merge commits, or that GitHub reports conflicting, is replayed: its own commits are cherry-picked onto its base (main, or the PR it is stacked on) and pushed with a lease. A replay that does not apply cleanly is marked `needs-hands` with the files, and left alone until an agent slot is free (below). GitHub's `UNKNOWN` mergeable state, which is it still computing, keeps the PR's previous conflict state.
3. **Batch.** PRs that have no passing result for their current head are grouped by the areas they touch: `android`, `console`, or `core` for everything else. A stacked PR rides with its parent, after it. Each group becomes one batch, at most two running at a time, none started when the load is over 150. A batch has an eight-hour wall clock, configured by `NZFLEET_BATCH_WALL_CLOCK` (seconds). It merges its members onto main, skipping any that conflict, and runs each area of their union the way ci runs it: in the image of ci's job for that area, with the job's Postgres service beside it and the job's own setup steps, and on the host only where ci has no image (`android`, `image`, `formal`). Docker is available inside a batch only to the areas that run on the host, as in ci. The jobs are read from the merged tree's `.github/workflows/ci.yaml`, so a ci change is checked by the job it wrote; a step a batch has no stand-in for is an error, not a difference.
4. **Bisect.** Only a batch the daemon formed does this, and not while a newer running batch covers any of its PRs; a manual batch (`nzfleet batch`, a train's) stops at its first red verdict as `red-once`. A red batch runs again unchanged. If it passes the second time, the failure was a flake and is recorded with the test names in the flake ledger (`state.json`, shown by `nzfleet flakes`). A test that reaches two flakes is queued once as a `flake-<slug>` task for a sonnet agent, with its failing log lines. A property test's counterexample is never counted: it is its own verdict. If it fails again, prefixes are tried by halving until the first failing member is found; that member is marked failed with its log excerpt, and the rest are checked again without it.
5. **Ready.** A PR is ready while its head is the SHA that passed, and main has moved since only by merging members of that same batch that came before it. Ready PRs are marked ready on GitHub and listed in merge order.
6. **Tell.** One issue, "Merge queue", holds the current `nzfleet land <train>` command in its body. A comment, which GitHub emails, is added only when the list gains a PR or a new train is recorded.
7. **Observe deploy.** The `deploy.yaml` workflow alone builds and rolls the API, console and site, signing the site's APK from CI's build. Nzfleet observes gated runs using GitHub's display name, `Confirm successful main push`, and reports completed success or failure once per workflow run. Its first pass reports only the latest gated run, leaving historical runs quiet. `nzfleet status` still reads the API's deployed commit.
8. **Machine.** Under 40 GB free on `/`, root cron runs the low-disk prune under a nonblocking lock and a ten-minute timeout. Every 15 minutes, tidy removes idle build output, old unused Postgres containers, inactive worktrees and old Robolectric unpacks. A build directory is kept while a live process has it as its working directory, names it in an argument, or has a file open inside it. `CARGO_TARGET_DIR`, `DATABASE_URL`, live batches and processes keep their outputs, databases and worktrees. Directories a container left owned by root are removed through docker. Failures go to `tick.log`.

9. **Agents.** A run whose log has no final report (a `PR: #n` or `PR: none` line, or a PR number in its last lines) is resumed in the worktree it left, up to three times, after 2, 10 and 30 minutes, with `git log` against its base and `git status` in the brief. A task that used them all is stuck: the owner loop emails once with the worktree, the last log lines and what it needs, and the daily digest lists every stuck task and for how long.
10. **Supervise.** Every tick each running agent's process tree CPU is sampled. No file change for 30 minutes with not a tick of CPU is a hang: the agent is stopped and resumed. No commit or file change for 45 minutes while the CPU is idle is a stall: the agent is stopped and resumed once with "where are you, what is blocking", and a second stall emails the owner. A log's age says nothing, since an agent writes its log at the end. Every tick: a `yes`, `stress`, `stress-ng` or `timeout` around one, in any agent cgroup, older than ten minutes and not under `nzfleet starve` is stopped, an event names the agent that owned it, its worktree is told and its PR commented. Once an hour: of two running batches over the same PRs the older is stopped; the tidy pass runs and removes databases and worktrees with no live owner; a chain idle while the machine has room is explained, and a `PR: none` that left nothing in its worktree stops holding it; a ready set that was never emailed is emailed; and one health line goes to `events.log`. Only a need for a person is emailed.

## Warm slots

A green batch posts only `nzfleet/merge-check`. What main reuses is a pull request run's own `ci-proof` artifact (`nzfleet/selection/proof.py`), which needs no login or variable of ours.

A batch does not build from nothing. `bin/merge-check.sh` takes one of `NZFLEET_WARM_SLOTS` (three) slots under `$NZFLEET_HOME/warm`, holding a lock on it for the batch: a worktree and a build directory that the previous batch left. The worktree is reset to main and the pull requests merged onto it, so git rewrites only the files that differ, cargo's modification times stay right, and cargo, Gradle and npm compile and install what changed. Rust runs with incremental compilation there; `scripts/checks.sh` touches the workspace's own sources first so every query is checked against this batch's schema again. A batch that finds every slot held for `NZFLEET_WARM_WAIT` seconds builds in a fresh worktree as it used to. `NZFLEET_WARM=` (empty) turns slots off. A new Rust toolchain, or a build directory over `NZFLEET_WARM_MAX_GB`, empties it.

| Cache | Size | Pruned | Clear |
|---|---|---|---|
| `warm/slot-N/target` | up to `NZFLEET_WARM_MAX_GB` (60); the tidy pass caps it at 80 when the slot's lock is free | tidy pass | `rm -rf $NZFLEET_HOME/warm/slot-N/target` |
| `sccache` | 60 (sccache), 80 (tidy) | tidy pass | `rm -rf $NZFLEET_HOME/sccache` |
| `cache/npm`, `cache/cargo-registry` | 8, 20 | tidy pass | `rm -rf` |
| Gradle, `~/.gradle` | Gradle's | Gradle | `rm -rf ~/.gradle/caches/build-cache-1` |

`nzfleet setup` makes the directories, installs and runs sccache, and prints the one-time root steps for the CI
runners; run again it changes nothing. A warm batch shows in its log as `warm: <slot>` and in each area's `stats:` line
as sccache hits against misses; `warm: no slot, building cold` means every slot was held.

Dependencies from a registry are compiled through one sccache in `$NZFLEET_HOME/sccache`, shared by every slot and area (`scripts/rustc-wrapper.sh`, `tools/nzfleet/runner/install-sccache.sh`), and npm reads its tarballs from `$NZFLEET_HOME/cache/npm`. Each area ends with a `stats:` line, and `result.json` and the batch in `state.json` carry the same figures: wall seconds, CPU seconds read from the cgroups, and sccache hits and misses.

## Files

- `nzfleet/plan.py`: every decision as pure functions. These are what the tests cover.
- `nzfleet/world.py` (git and GitHub), `nzfleet/cloud.py` (Cloud Run), `nzfleet/machine.py` (disk, load, containers), `nzfleet/shape.py` (replays) and `nzfleet/agents.py` (headless agents): the outside world, in thin wrappers.
- `nzfleet/train.py`, `nzfleet/land.py`: the verified train as plain data, and the command that lands it.
- `nzfleet/batch.py`: runs one batch and its bisection, by calling `bin/merge-check.sh`.
- `nzfleet/areas.py`: runs a batch's areas in ci's images (docker, thin); `plan.job_for` decides what each job is.
- `nzfleet/owner.py` (pure) and `nzfleet/owner_tick.py`, `nzfleet/mail.py` (thin): the emails and events of the owner loop; see the nzfleet skill.
- `nzfleet/tidy.py` (pure: what to remove) and `nzfleet/janitor.py` (reads the machine, removes it): the tidy pass. `nzfleet/setup.py` and `bin/env.template`: `nzfleet setup`.
- `nzfleet/runs.py` (a task's runs and resumption rules), `nzfleet/progress.py` (stall rules, chain metrics, tables) and `nzfleet/supervise.py` (what the hourly pass decides): pure. `nzfleet/observe.py` (process table, worktree activity), `nzfleet/supervise_tick.py` and `nzfleet/starve.py`: the thin parts.
- `nzfleet/journeys_plan.py` (pure: when a merge starts the phone journeys, what a run does, which findings become issues) and `nzfleet/journeys_run.py` (the thin part): see `docs/journeys.md`.
- `nzfleet/state.py`: `state.json`, read and written under an exclusive lock.
- `nzfleet/tick.py` and `nzfleet/cli.py`: the loop and the commands.

Tests: `python3 -m unittest discover tests`.

Every Claude agent nzfleet launches gets `--settings` with a PreToolUse hook (`hooks/worker-bash.py`) that denies Bash with `run_in_background`, because the session exits with its final message and takes the command with it, and any `git stash`, because the stash is one ref shared by every worktree and a pop can apply another agent's work. To make a test fail first, a worker saves its change with `git diff -- <paths> > $TMPDIR/x.patch && git checkout -- <paths>` and restores it with `git apply`.

## A fresh machine

1. Clone the repository with an `upstream` remote for nzela-org/nzela and run `tools/nzfleet/bin/nzfleet setup`.
2. It writes `~/.config/nzfleet/env` from `bin/env.template`, once. Fill in what is commented out; each variable is explained there, and the file holds paths and names, never secrets (the deployer's key is a file whose path goes in `NZFLEET_GCP_KEY`).
3. It makes the daemon's detached worktree of main, installs and enables `nzfleet.service` and the watchdog timer, and links `nzfleet`, `agent-db`, `gradle-slot` and the nzfleet skill into the user's directories.
4. It lists what is missing: docker, rustup 1.98, node 22, the Android SDK, gcloud, `gh auth login`, and the phones' adb host. Fix, run it again, until it lists nothing.
5. As root, once, it only prints: `sudo install -m 755 tools/nzfleet/runner/runner-prune.sh /usr/local/sbin/nzela-runner-prune`, the locked, ten-minute root cron line for the low-disk check, and the sudoers rule that lets nzfleet run `nzela-runner-prune check`. Check the rule with `visudo -cf` before installing it.

No user crontab entry is needed: the daemon does the janitor's work. `notify.py` (the owner's mail) stays machine-local; `NZFLEET_NOTIFY` names it.

The env file is read in two ways: systemd loads it into the service, and `config.py` loads it for a command typed by hand. It must therefore stay plain `KEY=value` lines, unquoted. A value with spaces in it is fine to systemd, but it cannot be sourced by a shell. A variable that is set but empty overrides a default, so an unused one is commented out.

State, logs, batch runs and agent output go to `NZFLEET_HOME`, outside the repository. The daemon runs from a detached worktree of main named by `NZFLEET_CODE`, and when main's ci is green on a commit that changes `tools/nzfleet` it moves that worktree to it between batches, runs nzfleet's own tests there, and only then re-executes itself on the new code. A failing test puts the old commit back and tells the owner on #695. The `nzfleet` command on PATH is a link into the same worktree; `nzfleet setup` makes it so.

A second unit, `bin/nzfleet-watchdog.timer`, runs `nzfleet watchdog` every ten minutes: when the daemon is not active or `tick.log` is fifteen minutes stale it opens (or updates) the issue "nzfleet is down" with the last 20 log lines, and closes it once the daemon is healthy.
`nzfleet setup` installs it.

## For an agent, any agent

Run `nzfleet next`. It prints one instruction: the most useful thing to do now, skipping work another agent already holds. Do it, then run `nzfleet tick`. `nzfleet status` explains everything else. Nothing needs remembering between runs: GitHub, git and `state.json` hold it all.

The daemon starts agents itself for three things, all within `NZFLEET_AGENT_SLOTS` (a rebase that needs hands goes ahead of queued work): an owner comment on a PR or issue, a PR whose replay needs hands, and a main that fails twice in a row in code. It uses `claude -p` by default. Set `NZFLEET_AGENT` to select any configured provider command, with `{model}` and `{brief}` filled in. `nzfleet session <name> --provider codex` selects Codex with `gpt-6-luna` by default; `--model <id>` overrides it, `NZFLEET_CODEX_MODEL` changes the default, and `NZFLEET_AGENT_CODEX` customizes the command. In the systemd env file, quote a command assignment that contains spaces, for example `NZFLEET_AGENT_CODEX="codex exec --model {model} {brief}"`; inner quotes group arguments, and the command runs without a shell. Use `--provider claude --model <id>` for a Claude session or `nzfleet session --clear` to return to `NZFLEET_AGENT`. A single task can choose for itself: `nzfleet todo <key> <brief> --provider codex|claude --model <id>` stores the choice on the task and the daemon uses it when it starts that task, whatever the session chose, including when the run is retried. `--model gpt-6-*` alone implies Codex and `sonnet`, `haiku`, `opus` or `claude-*` imply Claude; a task with neither follows the session. `nzfleet todo <key> <brief> --compare codex:gpt-6-luna,claude:sonnet` queues the brief once per worker, each in its own lane and branch, each opening a draft PR whose title ends with its model, so the results can be compared on real work. `nzfleet status` reports the provider and model recorded for each run and for queued tasks that chose one. Codex runs are not in Claude transcripts, so the cost lines price Claude only and count Codex runs separately.

## The agent kit

Four commands do the error-prone parts of a task, and each refuses with a sentence saying what to do next.

1. `nzfleet start <slug>` makes the worktree under `NZFLEET_WORK` at a pinned base SHA (`--base 816` stacks on that PR's head), on a branch named for the slug. It gives the worktree a block of twenty ports, recorded in `state.json` so two agents never share one, and starts a migrated Postgres on the block's second port. `node_modules` is installed once per `package-lock.json` hash into `$NZFLEET_HOME/cache/node` and hard-linked into each worktree. It writes `.nzenv` (database, ports, `CARGO_TARGET_DIR` under `NZFLEET_TARGETS`, toolchain `PATH`, author identity) and prints `cd <wt> && . ./.nzenv`. Run again with the same slug, it carries on where it stopped; a first install longer than nine minutes exits 3 and is finished by running the same command again.
2. `nzfleet prove` maps `git diff` against the base to checks in `plan.proof_plan`: fmt, clippy and the tests of the touched crates and modules; the console's typecheck, unit tests and touched Playwright specs; the touched Android test classes under the gradle slot; screenshots redrawn when pixels moved. `nzfleet pr` refuses PNG changes outside the screens the diff touches unless `--shots-anyway` is given. It never runs a whole `checks.sh` area. It stops at the first failure, prints a progress line at least every minute, and keeps each check's output and a `summary.json` in `.nzproof`. The summary records the tree of the files it ran on, so a later commit that differs from them makes the proof stale.
3. `nzfleet run <name> -- <cmd>` starts a command in its own process group with a pidfile, a log and an exit file under `.nzrun`. `nzfleet wait <name>` waits at most nine minutes, printing a line every minute, and exits 0 when it ended or 3 when it is still running. `nzfleet kill <name>` signals only that group, and only when its working directory is inside the worktree.
4. `nzfleet pr` refuses an empty diff, a missing, failed or stale proof, uncommitted changes, merge commits, and any commit, title or body carrying a Co-Authored-By trailer, a "Generated with" line or a session link. Otherwise it pushes with `--no-verify` (and a lease when the branch is already on the fork) and opens or updates the draft PR, whose body is the agent's description plus each check's command and output from `.nzproof`.

An agent run whose log ends with nothing but the tool's own warnings, or with words about work it left in the background, is marked `unfinished` and started once more, told to continue from the worktree it left.

## For the owner

Watch the "Merge queue" issue. Its body is always the command to run; a new comment means the list grew.

## The work queue

`nzfleet todo` lists work still to do. Finished entries leave the dispatch queue and remain in task history; the last completed task in each area still gates the next item until its PR passes or merges. `nzfleet todo <key> <brief or file> [--model sonnet] [--after k1,k2] [--area name]` adds a task. Each tick starts the oldest task whose prerequisites have finished, while fewer than `NZFLEET_AGENT_SLOTS` agents are running and the fleet is using less than `NZFLEET_MAX_CPU_PERCENT` of its CPU quota (the nearest `cpu.max` above the daemon, measured over at least 30 seconds; the load average against the same share of the CPUs where there is none). Both controls are refreshed from the env file each tick; the percentage defaults to 90 and cannot exceed 100. The older `NZFLEET_MAX_LOAD` is still read as a load while the percentage is unset.

An area is a free-form name for a part of the code. Items in one area run one at a time, in queue order, so two agents never work on the same part in parallel. When an agent finishes, nzfleet records the PR from the last `PR: #<n>` or `PR: none` line of its final message, and only when that line is missing guesses from the branch it checked out or named in its log. The next item in the area starts once that PR has passed a batch, on top of its branch with a brief saying to stack on it, or on main if the PR has merged. A guessed PR, a `PR: none`, or no PR at all stops the area and `nzfleet status` says which; `nzfleet attach <key> <pr>` records the right one. Different areas, and items with no area, run in parallel.

`--after` takes task keys, which must have finished, and `pr:<n>`, which waits for that pull request to have merged. `nzfleet hold <key|group> --why "main is red"` keeps a queued task, or every new batch of a group, from starting until `nzfleet release`; a batch already running is left to finish, and a held task keeps the rest of its area waiting behind it. Holds live in `state.json` and are listed by `status`.
