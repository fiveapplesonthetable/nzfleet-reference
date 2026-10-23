# For the agent building shunt from this code

This is a reference copy of nzfleet, the orchestrator that runs nzela's agent fleet. It is not maintained here. Read it to learn what shunt has to do and why. Do not port it file by file.

nzfleet grew over six weeks, one incident at a time. Most of it is right, some of it is scar tissue, and a few parts are mistakes that were later undone. This file says which is which.

## How to work

1. Read `nzfleet/plan.py`, `progress.py`, `supervise.py`, `runs.py`, `dispatch.py` and `holds.py` first. They are the decisions, as pure functions of plain data. Everything else is hands that read the machine or carry the decisions out.
2. For each nzfleet command you replace, write a parity test first: the same state and observation in, the same actions out. The tests under `tests/` show the cases nzfleet learned the hard way. A case there with no counterpart in shunt is a regression waiting to happen.
3. Keep shunt generic. The area list, the checks command, the screenshot rules, the phone journeys, the brief preamble and the Postgres recipe are nzela's and belong in project config or a plugin. Shunt should run a fleet on a second repository with no nzela word in its code.
4. Replace one command at a time. nzfleet's command becomes a thin call into shunt once its parity test passes, so the fleet keeps running throughout and any step rolls back.

## Copy these. They were paid for.

- **Decisions are pure; hands are thin.** `plan.py` never touches git, GitHub or disk, and `world.py` never decides. That is why nzfleet's behaviour can be tested at all. Keep the split, and make shunt's `decide` replayable from its log.
- **Every action carries its reason.** The status screen and the owner's emails explain themselves because each decision records why. Put the reason in the event.
- **A worktree is pinned to a base SHA, not to a branch name.** A stale `origin/main` on a fork once cost two full agent runs.
- **One Postgres per agent.** A shared one made parallel sqlx tests drop each other's databases.
- **Batches run CI's own jobs.** `areas.py` reads `.github/workflows/ci.yaml` and runs each job in its image with its services. It refuses any env expression it has no value for, rather than guessing. Keep that strictness: it is what stops a batch passing a tree CI would fail.
- **A merge check refuses a result if main or a PR head moved while it ran.** Main broke three times on branches that were each green, with no textual conflict between them. Checking the merged tree is the point.
- **A train compiles Rust and Android on the merged tree before it goes out.** A textual merge plus a type check missed five breaks in one day.
- **A worker may only stop processes it started.** `procs.py` names them and keeps them under the worktree. Select processes by argv fields or cgroup, never `pkill -f`, which matches the tool's own shell.
- **CPU-heavy commands run under `starve`, and a hook refuses them otherwise.** Without it, one worker's build took the machine from everyone else.
- **The owner is told once.** `attention.py` gives one line per item with the command that fixes it, said once rather than every tick.
- **Self-update moves the running code to main only when main is green.**
- **Redaction runs on everything that leaves the process:** page, issue, email and log tail.
- **Tidy never prunes Docker while a build runs and never touches the owner's home directory.**

## Do not copy these

- **Time-only stall detection.** nzfleet's supervisor and the session watcher beside it called a worker stalled after 45 minutes with nothing written and an idle CPU of its own. That stopped workers that were waiting on a long check in a child process. Progress must count child processes, commits and file changes, not only the agent's own output.
- **Hourly supervision and polling.** `supervise_tick` looks once an hour, and the orchestrating session polls main with shell loops that have to be re-armed by hand. A two-hour silent gap once left agents paused. Shunt records events, so react to them as they arrive.
- **The 3-hour `timeout` plus a "continue" resume.** Every worker runs under `timeout 10800` and is restarted with a continue brief. That works around missing provider control. Build real resume and `tell` instead, and keep a time limit only as a backstop.
- **Messaging an agent by stopping and restarting it.** There is no `tell` today, and every nudge costs a cold restart.
- **The mixed `plan.py` and `tick.py`.** `plan.py` holds dispatch, batch, train and environment decisions in 1,215 lines, and `tick.py` does nine jobs in one pass. The pure/impure split is right; the grouping is not. Split decisions by concern: dispatch, verify, land, watch.
- **Workers running their own batches or merge checks.** nzela tried briefs that told workers to batch and time their own checks. Only 29% of batches were green on the worst day. Verification belongs to the orchestrator, once, on the merged tree.
- **"Land first, fix after."** It was tried and retired. Only exact verified trains merge, in order, with nothing in between. The owner can still choose to merge fast and fix forward; that is a human call, not a default.
- **Re-running a red area and calling it a flake.** A failure seen twice gets read, reproduced and fixed within the hour. A day was lost to re-runs once. Shunt can record that a test failed and passed on retry, but it must never treat that as success.
- **Screenshots as a merge gate.** Stale or drifted shots caused most of nzela's red builds without a product bug behind any of them. Now only a small golden set is strict, and the rest are redrawn on the merged tree and reported.
- **nzfleet re-checking main after CI already did.** It duplicated CI's work and competed with product batches for the machine. Trust CI's result on main and spend the machine on PRs.
- **The fork-sync code.** Paths in `plan.py`, `tick.py`, `world.py`, `openpr.py`, `trainpr.py`, `config.py`, `setup.py` and `agents.py`, plus `tests/test_fork_sync.py`, keep a fork's branches in step so they get CI. The upstream repository now runs pull requests on its own runners, so none of this is needed.
- **Jev.** `jev.py`, `jev_points.py` and `triage.py` shadow decisions with a cheap model. It is research that never became a decision path. Leave it out.
- **Model choice in brief text.** Which model writes what, such as sol for product, luna for mechanical work and Sonnet for review, lives partly in briefs and memory. Make it provider config with a per-subscription fallback.
- **Stopping product work to lower load.** Load is how the fixes for flaky tests get proven. Cap a worker's share with cgroups; do not stop it.
- **Anything nzela-specific in core code.** The deploy watcher, phone journeys, warm Gradle slots, golden screenshot lists and the issue titles "main is red" and "Screenshots to refresh" are examples. They are plugins.

## The rules every worker follows

These came from incidents, and shunt should enforce them rather than trust each brief to repeat them:

- No tool attribution in commits or PRs: no Co-Authored-By trailer, no "generated with" line.
- No single test is time-based. A test never waits a set time, never asserts a deadline or a speed, and never sleeps; it waits for the state it needs and asserts order or state, so it passes or fails the same way on a loaded machine. Benchmarks are not tests and do not run in the suite. A raised timeout is never a fix.
- Every suite, harness and batch area does have one overall time cap, so nothing can hang forever. Hitting the cap is reported as a hang, naming the test that was running and the last thing it printed, never as a pass or a quiet cancellation.
- Never `git stash` in a shared checkout; worktrees share the stash.
- Never freeze or kill a worker that holds a Gradle or cargo lock.
- Keep every foreground command under 10 minutes, and run longer ones in the background with a notification.
- Credentials are read from files and never printed, echoed, or written to the log.
- The owner merges. Agents open PRs and never merge or ask for write access.

## Where shunt stands at ffe48a5

This was checked on 23 October, by reading the code.

**Fixed since 1f6288a:**
- Batch reads a real workflow. `parse_workflow_yaml` (`verify.rs:2155`) refuses tabs, unclosed quotes and bad mappings, and `expand_workflow_expr` (`verify.rs:1721-1747`) refuses any `${{ }}` it has no value for. Unsupported `if:`, `uses:` and `working-directory` fail too. The silent `cargo test` fallback is gone.
- A batch re-reads main and the PR heads after it runs and refuses the result as `head-moved` if any moved (`fleet.rs:1405-1442`).
- A train always runs a compile command and refuses an empty one (`fleet.rs:1849-1880`).
- `tick` calls `decide` with plain-data inputs (`fleet.rs:2588`).
- `FdEventWatcher` (`kit.rs:792-875`) waits on inotify, pidfd and `poll(2)`.
- The provider trait (`dispatch.rs:1495`) has start, resume, tell, alive, stop and cost, driven from `default_providers.json` with real CLI flags, and cost reads the CLI's own usage.
- A retry that passes is recorded as a flake and never as a pass (`fleet.rs:1448-1486`), and repeated flakes queue a fix task.
- Stall detection counts log growth, HEAD changes, dirty files and the process tree's CPU (`watch.rs:268`).
- No nzela word is in the crates.

What remains, most important first:

1. **The train compiles the wrong tree.** The compile command gets `TACHO_TRAIN_SLICE_PRS` as an environment variable, but nothing builds the merged tree for the slice, so the default `cargo check --workspace` checks whatever is checked out. Build the merged tree in a scratch worktree, compile there, and refuse if main or a head moved.
2. **Time-based tests remain.** The speed asserts are gone, but `tacho-e2e/src/lib.rs:285-302` (`wait_until`, `wait_for_api_sock`) waits with timeouts, and sleeps and deadlines remain in about 25 test files, for example `test5_daemon_restart.rs`, `test6_watch_and_report.rs:238`, `test1_replay_sequences.rs:93` and a 5 s polling deadline in `appender.rs:763-878`. No single test may be time-based: wait on the state (inotify, a pidfd, a socket's own readiness) and assert order or state. Each suite gets one overall cap that reports a hang, naming the test. The benches print timings and belong outside the test suite.
3. **The daemon never decides.** Nothing in `tacho-daemon` calls `decide`, so `tacho tick` needs an outside caller. `batch_outcomes` is passed empty (`fleet.rs:~2582`). `tell`, `kill_burner` and `remove_worktree` still act outside `decide` (`fleet.rs:2271,2425`). Only decisions are logged, not their inputs, so a tick cannot be replayed; `tacho replay` re-runs a batch against the live tree instead. Log the inputs, make replay feed them back to `decide`, and have the daemon run it on each event.
4. **Polling remains outside the watcher.** `thread::sleep` loops are in `tacho-system/src/lib.rs:309,345,359`, `server.rs:202,222,566`, `cgroup.rs:106`, `otlp_server.rs:87`, `appender.rs:211,383`, `sampler.rs:1057` and `cli/daemon.rs:36`. Move each onto `FdEventWatcher`, or say why it must sample.
5. **`tell` to a live Claude still goes through the hook inbox.** Claude's `tell_program` is null, so a live session gets the message on its next hook turn. The live-resume path kills the process group and respawns, and it falls back to `resume_at` without saying so when the worktree or log is missing (`fleet.rs:~790-815`). Make the fallback an error, and define what a provider that cannot take a live message does.
6. **Process kills trust a pid.** The tell and kill paths call `libc::kill(-pid)` on a pid that may have been reused. Check the pid still belongs to the agent (its cgroup or its start time) before signalling.
7. **Smaller gaps:**
   - Confirm a `flake` batch is never read as a pass by `queue` or `decide`. The flake path returns `Ok(())`.
   - Checks are not modelled as area plus affected units.
   - There is no presubmit or postsubmit mode.

## Still changing in the live nzfleet

The live copy stays in nzela and keeps running the fleet until shunt replaces it, so this snapshot will fall behind. These changes were in flight when it was taken. Design for where each is going, not for what this copy shows:

- **Fork sync is going away.** Pull requests now run on the upstream repository's own PR-tier runners, and fork pushes no longer get CI. Do not build the fork-sync paths.
- **Batches take one more CI expression.** `areas.py` now supplies `CEILING_BASE` alongside `NZELA_BASE`. Expect more of these, and keep the refuse-if-unknown rule rather than a fixed list.
- **Rust and Android checks are becoming incremental.** Rust checks only the crates a change reaches, in a warm target. Android builds and tests by Gradle module. The goal is seconds locally with warm build servers. An area's checks will become "the affected units of this area", so model a check as area plus affected units, not area alone.
- **The crate split is in its last steps.** Contract suites on every port, an in-process simulator and a split load driver are coming. They change which checks a change selects.
- **Stall detection is being fixed** to count child processes, after workers waiting on long checks were stopped as stalled. Shunt should start from the fixed behaviour.
- **A console browser test run that hangs** after its tests pass is being fixed. A batch area must have a cap, and hitting the cap must be reported as a hang with the last thing the job printed, not as a cancellation.
- **Presubmit and postsubmit are being split.** Before merge runs a subset chosen by the change, after merge runs everything, and a full gate runs before real-money production. Shunt's batch should support both modes.
- **Compromises are tracked in nzela issue #1787** and are being worked through. Several touch nzfleet's checks.

When the snapshot is refreshed, this section is updated with it.

## What to build first

1. Make shunt's own tests green with no environment set, and turn the scrubber back on by default.
2. Replace the stall watcher (`progress`, `supervise`, `observe`), with the child-process and commit signals above, beside nzfleet for a few days.
3. Build the provider interface with start, resume, `tell`, stop and cost.
4. Build `decide` as one replayable function, then batch and trains, then the watchers.

The plan with sizes and test bars for each step is in the PDF the owner has, "What shunt needs to replace nzfleet".
