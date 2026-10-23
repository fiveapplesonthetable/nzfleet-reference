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

## Where shunt stands at 03135fc

This was checked on 23 October, by reading the code and running the suite with a clean environment.

**Fixed since ffe48a5:**
- The moved-ref refusal works when origin has no copy of a branch: live heads fall back to `refs/heads/<branch>` and main to `refs/heads/main` (`fleet.rs:1160,1220-1233`), and its test moves a real branch instead of editing the database.
- A train compiles each slice in a scratch worktree at the starting main with every PR head merged in (`fleet.rs:2024-2092`), then refuses if main or a head moved (`fleet.rs:2122-2134`).
- Decision inputs and plans are logged to `decision_ticks`, and `batch_outcomes` and burners are filled in.
- Most `thread::sleep` loops in the daemon are replaced by `poll`, pidfd and signal waits.
- A live resume with a missing worktree or log is now an error, not a quiet fallback.
- `still_ready` lands only a batch whose status is `passed` (`land.rs:91`).

What remains, most important first:

1. **The suite is redder than before.** With a clean environment, on a machine busy with other builds, `cargo test --workspace --no-fail-fast` gives 215 passed and 11 failed (ffe48a5 had 2): `prop_oracle_verification`, `prop_random_track_keys_oracle` (`recorder_prop.rs:506`), `test_on_stall_hook_timeout_kills_process_group_and_unblocks_second_hook`, both `tail` tests (partial line, truncation and rotation), `test_daemon_restart_lifecycle`, `test_fleet_tick_dispatch_reap_land_mismatch_and_tidy_rescue`, `test_kit_job_wait_and_kill`, `test_multi_agent_git_chaos`, `test_strict_start_base_db_plugin_live_tell_and_batch_head_moved` and `test_torture_daemon_chaos_and_recovery`. Failing under load is what time-based tests do. Make them wait on state, then run the suite five times beside a heavy build, and it must be green every time.
2. **Time-based tests remain.** About 58 `thread::sleep` calls in 12 test files (18 in `test_torture.rs`), `wait_until` or deadlines in 14 files, and a timeout in `wait_for_api_sock` (`tacho-e2e/src/lib.rs:320-373`). No single test may wait on time; wait on the state and assert order or state. One overall cap per suite is fine.
3. **The daemon still does not decide on its own.** It calls `decide` only on a "tick" or "decide" API command (`server.rs:491-542`), and that path logs an empty `main_sha` and no batch outcomes, which poisons replay. `tell_actions` is always empty (`decide/mod.rs:553`); `tell` (`fleet.rs:864`) and worktree removal (`fleet.rs:2886-2899`) act outside `decide`. Every tick feeds all finished batches back in, flakes included, and `decision_ticks` is never pruned.
4. **Killing a process still trusts a pid.** `verify_agent_pid` (`fleet.rs:106`) is never given a recorded start time, returns true when `/proc/<pid>/stat` cannot be read, and accepts any process whose command line contains "claude", "tacho", "gemini" or "codex". Burner kills are unconditional after 600 s (`fleet.rs:2866`). Record each agent's pid with its start time or cgroup and refuse anything that does not match.
5. **`tacho replay` regressed.** An id that is not a tick falls back to the latest tick (`fleet.rs:1836-1850`), so batch replay is unreachable and a numeric batch id is read as a tick id. Its `assert_eq!` panics instead of returning an error.
6. **Errors are dropped.** `record_decision_tick` and the worktree removal in `tick` discard errors with `let _ =`. The train's scratch worktree is left behind when `Command::output()` fails (`fleet.rs:2063,2088`).
7. **The tidy rescue runs `git stash`** and then force-removes the worktree. Worktrees share one stash, so this can lose or mix another agent's work. Commit to a rescue branch instead.
8. **Smaller gaps:**
   - A live Claude still gets `tell` through the hook inbox on its next turn.
   - Timed `poll(…, 100)` loops remain in `server.rs` and `otlp_server.rs`, and sleeps in `sampler.rs:1060`, `appender.rs:389`, `system/lib.rs:311`, `client.rs:85` and `tail.rs:198`.
   - Confirm the queue itself never reuses a `flake` batch as a pass.
   - Checks are still not modelled as area plus affected units.

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
