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

## Where shunt stands at 5ba1052

Checked on 24 October by reading the code against this file and running `cargo test --workspace --no-fail-fast` with no environment set, on a machine busy with other builds. 745 passed and 9 failed, and the run then hung: `fleet::tests::rescue_fails_loudly_outside_a_repo_so_tidy_keeps_the_tree` was still running when the 50-minute suite cap stopped it. The failures:
- `decide::tests::test_pluggable_traits_and_project_policy_unification` expects `CARGO_TARGET_DIR=/cache/warm/slot-0/target` in the agent environment (`decide/tests.rs:1409`), so it depends on a path the test does not create.
- `decide::tests::test_multi_tick_adversarial_simulation_across_all_concerns`: no watchdog alert on the fifth plan (`decide/tests.rs:1712`).
- Both `appender::tests::test_on_stall_hook_*` panic on `inotify_init1 failed` (`tacho-daemon/src/test_util.rs:12`). A busy machine runs out of inotify instances; shunt has to fail with a clear error and never depend on one being free in a test.
- `test_real_cli_binary_end_to_end` (`test_cli_integration.rs:186`), `test_status_vs_agents_now_sql` (`test3_status_vs_sql.rs:109`), `test_watch_until_stalled_and_report` (`test6_watch_and_report.rs:76`), `test_multi_agent_git_chaos` (`test8_multi_agent_git_chaos.rs:542`) and one more e2e test.

A hang the cap catches must be reported with the test's name and last output, which is what the suite cap is for; here it only showed as the run being stopped.

**Fixed since 03135fc:**
- No `thread::sleep` or `tokio::time::sleep` is left anywhere. Tests wait on pidfd, inotify and `poll(-1)` (`tacho-e2e/src/lib.rs:309-580`), with one overall cap, `SUITE_SAFETY_CAP` (`lib.rs:453`).
- `tacho replay` checks a recorded batch, then a PR, then a tick, and an unknown id is `NotFound` (`shape.rs:316-420`). Divergence is an error, not a panic.
- The tidy rescue commits to `refs/tacho/rescue/<key>-<ts>` instead of `git stash`, and a test asserts the stash list stays empty.
- `tell` reaches a live Claude through the hook inbox (`session.rs:576`, `hook.rs:510-513`).
- A check is an area plus affected units (`verify/types.rs:20`, `plan.rs:146`), carried through batch and prove.
- `verify_agent_pid` fails closed when `/proc/<pid>/stat` cannot be read and checks the recorded start time; `verify_kill_target` checks start time, cwd and pgid.
- `handle_decide` refuses a tick without a `main_sha`, and `decision_ticks` is pruned hourly.

Everything below has to land for shunt to replace nzfleet. Land it all; there is no partial cut-over.

1. **The daemon decides on its own.** `decide` still runs only on an API "tick" or "decide" command (`server.rs:636`, `handle_decide` at `server.rs:816`) and on request from the CLI (`fleet/watch.rs:438`). The daemon must observe the world (agents, PRs, CI, main) on events and run `decide` itself, with every action, `tell` and worktree removal included, coming out of `decide`. Check that `tell_actions` is filled and nothing acts outside it.
2. **No kill without identity.** When no start time is recorded, `store.rs:140-175` still accepts any process whose cgroup, environment or command line contains the agent key, and `watch.rs:261` kills a duplicate batch with `verify_agent_pid(pid, dup_id, None, None)`. Record a start time or cgroup for every pid shunt may kill, and refuse a kill without one.
3. **No dropped errors where a caller can act.** `main.rs:189,313,425,443` (`record_decision`, `store.mutate`), `watch.rs:459` (decision on a burner kill), `kit.rs:274,295` (git commands), `train.rs:681` (the scratch worktree removal, so a failed cleanup is silent), `daemon.rs:305` (the tick prune) and the telemetry emits in `batch.rs:534,629,783,815`. Each is handled or returned.
4. **No per-test wait on time.** `wait_until(timeout, …)` (`lib.rs:310`) is a `yield_now` spin with a deadline; the deadline spins in `kit_and_fleet_tests.rs:569`, `run_tests.rs:48` and `queries.rs:1111`; the short timeouts set on the thing under test in `otlp_server_test.rs:407` (200 ms) and `appender/tests.rs:91` (300 ms); and the socket read timeouts in `test_torture.rs:264,283`. Wait on the state and assert order or state. `wait_for_api_sock` ignores its `Duration`; drop the argument. Confirm `test_torture.rs:1681-1778` reports timings without asserting a speed.
5. **No timed polls in the product.** `tail.rs:213` (500 ms), `client.rs:102` (250 ms), and `tail.rs:579`, a `poll(NULL, 0, ≤100)` used as a sleep when inotify is exhausted. Block on the event, or fail loudly when the event source is gone.
6. **Docs match the code.** `README.md:26` and `README.md:265` say the scrubber is off by default; `daemon/config.rs:35` turns it on, which is right. Model names such as `gpt-6-luna` belong in config, not in code defaults.
7. **The proof.** With no environment set, `cargo test --workspace --no-fail-fast` green five times in a row beside a heavy build on the same machine. Then run shunt beside nzfleet on the same fleet for a few days with `decide` logged, and compare the decisions.

## Still changing in the live nzfleet

The live copy stays in nzela and keeps running the fleet until shunt replaces it, so this snapshot falls behind. It is refreshed from nzela main whenever main's nzfleet changes, and this section moves with it. Design for where each item is going, not for what this copy shows.

Landed since the 25 Oct snapshot, so shunt should start from them:

- **Presubmit and postsubmit are split.** The pre-push hook runs only what a change can reach: Android test classes, console specs, deploy and fleet scripts. It prints its wall time. The full run on main starts after the deploy finishes, and the suites after a merge wait for the deploy so it never queues behind them.
- **Android unit tests run in four parallel shards** behind one compile job, and console source changes reach every browser spec.
- **Batch areas that share one checkout no longer break each other's cleanup.** Runner files go in the container's own temp, and a batch's shallow checkout holds the base commit its checks compare against.
- **Repair trains run a full batch when main's CI is unproven.**
- **One torn line in the decisions log no longer stops supervision.**
- **More tooling moved under `tools/nzfleet`:** backups and the restore drill (`backup/`), release and image scripts (`release/`), deploy checks (`deploycheck/`), screenshot planning (`shots/`) and the book build (`book/`). In shunt, each of these is a plugin, not core.

In flight now:

- **Accepting moved screenshots from a run** (`nzfleet shots accept <run>`). A batch already saves every before and after image under `runs/<run>/shots/<area>/{before,after}/<repo path>`. When a golden moves on purpose, the command copies that run's after images into the branch and stages them: by default only the goldens that moved, with `--all` every image that moved. It refuses when the run was drawn from another HEAD. A batch that fails on a moved golden prints the exact command. Without it, the image was copied by hand and the step was often missed. Shunt should have this from the start, as a generic "accept the artifacts this run produced" with the screenshot rules in project config.
- **Screenshot tag limits are per screen, each with its reason.** Golden screens are held exactly, and every other screen has a cap on the status tags it may show, plus one per row and one in the header. A cap is raised only with the reason written beside it. That is project config, not shunt core.
- **Rust checks are becoming incremental:** only the crates a change reaches, in a warm target. Model a check as an area plus its affected units.
- **CI toward five minutes to deploy and seconds locally** is the current priority. Expect the selection code (`nzfleet/selection/`) to keep changing.
- **Compromises are tracked in nzela issue #1787**, and several touch nzfleet's checks.

The code was last refreshed from nzela main on 27 Oct (ed9109b5b).

## What to build first

1. Make shunt's own tests green with no environment set, and turn the scrubber back on by default.
2. Replace the stall watcher (`progress`, `supervise`, `observe`), with the child-process and commit signals above, beside nzfleet for a few days.
3. Build the provider interface with start, resume, `tell`, stop and cost.
4. Build `decide` as one replayable function, then batch and trains, then the watchers.

The plan with sizes and test bars for each step is in the PDF the owner has, "What shunt needs to replace nzfleet".

## Where shunt stands at d2a1aa7 (reviewed 25 Oct, by reading only)

Four commits since 5ba1052, nearly all in one change: an autonomous decide loop in the daemon, strict kill identity, zero timed polls, and the docs brought in line.

Fixed: no kill without identity (item 2), no timed polls in the product for tail and client (item 5), docs match the code (item 6). Mostly fixed: the daemon decides on its own (item 1), no dropped errors (item 3; `let _ =` remains on `libc::kill` in watch.rs and on kit git calls), no per-test waits on time (item 4; `test_util.rs` still asserts `inotify_init1` succeeds). Still open: the proof (item 7), five green runs beside a heavy build and a shadow run next to nzfleet.

New blocker: stall detection in `execute_decide_tick` invents progress samples for a stalled agent (timestamps set back 2800 s and 900 s, made-up CPU ticks) and reads progress from timeline length and token counts with an empty head and no dirty-file count. Stall detection therefore still ignores child processes and commits, and a replayed decision is not what really happened. Smaller: the decide tick opens SQLite every second inside the sampler thread.

No upstream issues, pull requests or replies; discussions are off. Not ready to adopt. Next review: after the stall samples come from real process and git state, and the suite is run five times under load.

### Where shunt stands at edf6662 (reviewed 25 Oct, built and tested)


Read against the d2a1aa7 verdict, the nzfleet reference notes, and nzfleet as it runs today. Built with `cargo +1.98 build --workspace --tests --offline` (1 min 57 s) and run with `cargo +1.98 test --workspace --no-fail-fast --offline`, no `TACHO_*` set, at load 90 to 107. Nothing left the machine. Logs: kept locally.

### Verdict: not ready

The one blocker named at d2a1aa7 is untouched, and reading the daemon's new executor path turned up a data-loss path that the earlier reviews did not reach.

### Blockers, ranked

1. **Stall samples are still fabricated.** `crates/tacho-daemon/src/fleet.rs:1143-1183` is byte-for-byte what d2a1aa7 had: an agent the recorder flags gets invented history at `now - 2_800` and `now - 900` with `cpu_ticks` 0, 1, 2, which forces `verdict` (`watch/legacy.rs:254`) to `Stalled`; every other agent's "CPU" is `tokens_in + tokens_out` and its "log bytes" is `timeline.len() * 64`. Child processes never count, so a worker waiting on a long check is still stalled by construction, and the recorded tick replays a world that did not exist. The real process-tree sampler exists (`watch/sample.rs`, `tree_cpu` used by the CLI at `fleet/watch.rs:381-416`) but the daemon does not call it. The commit message does not claim this was fixed; nothing in the diff touches it.

2. **The daemon deletes worktrees it has declared clean without looking.** `fleet.rs:1219-1220` hard-codes `dirty: false, unpushed_commits: 0` for every worktree, and `age_secs` is `now - created_at` (1210), not idle time. `decide_tidy` (`watch/legacy_ops.rs:226`) therefore never asks for a rescue, and the executor (`fleet.rs:1266-1278`) matches `RemoveTree { path, because, .. }`, ignoring `rescue` anyway, then runs `git worktree remove --force` and `remove_dir_all` (1042, 1063). Any worktree older than an hour whose agent pid is gone (crashed, stopped, held) loses uncommitted and unpushed work. The CLI `tidy` has the rescue ref; the daemon, now the authoritative executor, does not.

3. **A force push whose result is thrown away, then state that says it worked.** `fleet/shape.rs:237-244`: `let _ = git push --force-with-lease …`, followed by `store.mutate` setting the PR head to `new_head` and `mergeable = MERGEABLE`. The lease is the local tracking ref, not the head observed from the API as nzfleet's `shape.py:62` pins it, so a push between the fetch and the replay is overwritten. Operator-triggered only (`tacho replay <pr>`, 384), but it is the exact "force-push over someone's work" case.

4. **Owner notifications go nowhere.** `plan.owner_notifications` are only written as `notify_owner` rows (`fleet/watch.rs:829`); `ops.rs:notify_owner` has no caller; `LandActionKind::SendMail` and `MergePr` have no hand in `tacho-cli` or `tacho-daemon`; `ops/watchdog.rs` composes the "is down" issue but no code calls `gh issue` (`Command::new("gh")` exists only in `kit/start.rs:118` and `kit/pr.rs:471`). nzfleet mails merge commands, opens "main is red" and "nzfleet is down", and lands a train on command. shunt decides all of it and does none.

5. **Tests still depend on a free inotify instance, and under load they fail.** 939 passed, 21 failed in 2 min 45 s. Sixteen failures are `inotify_init1 … Too many open files` (this host has 124 of 128 instances in use): `test_util.rs:14` and `tacho-e2e/src/lib.rs:600` now print a better message and panic, which is the "reworded" fix. `snapshots_under_load` died on the product's 2 s `SEND_TIMEOUT` at load 100. Three kit tests fail on bare `assert!(out.status.success())` with no stderr shown (`kit_and_fleet_tests.rs:256,744`, `kit/tests.rs:695`), so their cause cannot be read from the log. The e2e tests gated on `trace_processor_shell` (`queries.rs:128`, `test8:60`, `test7`, `tacho-system/tests/integration.rs:132`) pass silently when it is absent, which is a pass without the code they cover.

6. **Screenshots: goldens are auto-refreshed.** `verify/verdict.rs:209-250` (R23) refreshes goldens when a single PR fails only in pixel areas. nzela's rule is the reverse: the golden set is strict, the rest is redrawn and reported. The `tacho pr` stray-shot refusal (`kit/pr.rs:197`) does match nzfleet.

### Improved since d2a1aa7

- Decide runs on committed events plus a 60 s refresh, serialised and deduplicated (`fleet.rs:1067-1100`, `sampler/mod.rs:529`), replacing the per-second SQLite open.
- Burner kills go through a starttime-checked pidfd and refuse without identity (`fleet.rs:1378-1430`); `signal_group_and_leader` returns errors instead of `let _ = libc::kill`.
- Supervise resume classifies the recorded process and tells a live agent rather than respawning beside it.
- `main-broken` batches have a lifecycle (`verify/unbreak.rs`); private `TMPDIR` for jobs, provisioners and burners; warm cache layout in config; fork-sync removed; about 60 parity tests over pure functions, which are real tests.
- Batch areas run under `systemd-run … AllowedCPUs/MemoryMax` plus `taskset` (`ci_runner.rs:203-230`), at parity with `areas.py:315`.

### What stays at parity

Provider `start/resume/tell/stop/cost` (`dispatch/provider.rs:66-117`); bisect (`verify/bisect.rs`); train compile in a scratch worktree, default `cargo check --workspace` (`train.rs:752`); area selection by configured path rules with fail-closed unplaced files (`areas.rs:44`); scrubber on by default.

### Migration path, when the blockers are gone

Not yet. Items 1 and 2 are the daemon's two hands with the worst failure modes and both run unattended; 4 means the owner would hear nothing. Next review after the daemon samples real process trees and git state, tidy rescues before removal, and the suite is green five times under load with every skip turned into a failure or a listed exclusion.

## Where shunt stands at a4f672c (reviewed 25 Oct, built and tested)


Built with `cargo +1.98 build --workspace --tests --offline` (2 min 27 s, clean); tests at load 85 to 140 with 125 of 128 inotify instances in use. The full run hit the 10 minute cap because the daemon unit-test binary alone took 578 s, so the workspace ran as `--exclude tacho-daemon` (1 min 50 s) plus the daemon binaries: 967 passed, 2 failed. Nothing left the machine. Logs: kept locally.

### Verdict: not ready

Four of six are fixed or close. What stops it: a git bug that hit this host during the run, rescue-less deletions, and a merge path.

### Earlier blockers

1. **Stall samples: fixed.** `exec/observe.rs:29-97` reads `/proc/<pid>/stat`, `cmdline`, `cwd`; `agent_cpu_ticks` (102-127) sums the tree plus the `tacho-*.scope` cgroup only (`cgroup.rs:175-184`); `progress_sample` (188-210) returns `None` without a verified pid, and `decide/mod.rs:250` then takes no verdict. The daemon runs the same code (`sampler/mod.rs:523-529` → `exec/mod.rs:285-298`).

2. **Tidy: partly.** Age is idle time, unknown dirtiness counts as dirty (`observe.rs:285-295`), and `apply_tidy_decisions` rescues first and keeps the tree on failure (`tidy.rs:111-126`). Still removed unseen: `decide/mod.rs:566` queues `RemoveTree { rescue: false }` for a retired or merged agent's worktree `decide_tidy` skipped; `tidy.rs:128` ignores the `rescue` flag on `RemoveCheckout` (`legacy_ops.rs:246`); a non-git directory goes to `remove_dir_all` (`tidy.rs:180`).

3. **Force push: partly.** `shape.rs:242-267` pushes `--force-with-lease=<branch>:<pr.head>` and mutates the store only on success (274-283); the test at 464 proves a moved remote is refused. But `pr.head` is the cached `open_prs_cache` row (391-395), not observed at replay time, and an empty head falls back to the bare tracking-ref lease (243-244). It also pushes `--no-verify` (250), skipping nzela's hook.

4. **Notifications: partly.** `exec/mod.rs:743-760` runs `notify_owner_command` (scrubbed, `plugin.rs:887-927`) and `forge.set_issue` (scrubbed, 806-836), but every result is `let _ =`, so a failed mail is never recorded or retried, and the daemon loop never sets `opts.forge` (`sampler/mod.rs:523`, `fleet.rs:990`; only `watch.rs:144-152` does), so the daemon cannot open "main is red".

5. **inotify and orphans: fixed.** Zero inotify failures at 125/128 (`e2e/lib.rs:586`, `test_util.rs:12` fall back to poll); no daemon left behind. Two daemon unit tests fail: `test_daemon_autonomously_runs_decide_without_api_tick` passes alone in 1.4 s (load flake: `sample_now()` returns before the tick sees the run); the other is the bug below, whose hang holds the process-wide `TICK_LOCK` (`exec/mod.rs:266`) and stalls every other tick test. `queries.rs:127` now requires `trace_processor_shell`.

6. **Golden screenshots: fixed.** `verdict.rs`/`machine.rs`: refresh only on `Green`, non-golden only; a golden diff stays `Red`.

### New blockers

- **Git is not confined to the worktree.** `observe_worktree` (157-171) and `tree_of_worktree` (`tidy.rs:229-306`) run git in `dir` with no toplevel check. A recorded path that is a plain directory resolves to the enclosing repository: its status makes the tree "dirty", and the rescue runs `git add -A` over that whole repo with no timeout, then `update-ref refs/heads/rescue/…` in it. Observed: the fleet test (`fleet.rs:1164`) ran `git add -A` across all of `/srv/fleet` for six minutes, and `run_git`'s SIGKILL at 5 s (`sampler/worktree.rs:241-244`) left `/srv/fleet/.git/index.lock` (removed by hand). On nzela that kill on each live worktree's `git status` every tick leaves `index.lock` files that break the agent's next commit.
- **`tacho land` merges.** `land.rs:111-116` runs `gh pr ready` then `gh pr merge --merge` (`plugin.rs:870-882`) for every PR of the train. Only the CLI calls it (`main.rs:466`), never the daemon, but nothing disables it: the gate is `TACHO_GH_REPO`, which also enables issues. The owner merges by hand; this needs an off switch.
- No new credential path: `gh` uses ambient auth and bodies are scrubbed; `TACHO_NOTIFY` is the one `sh -c` (`plugin.rs:902`).

Next review once git refuses a directory whose toplevel is not itself, the rescue-less removals go, merging is off by config, and the suite is green five times under load in ten minutes.

## Where shunt stands at 31bd756

Ready with two blockers. All four items left open at a4f672c are fixed in code, and each has a test that fails without the fix.

- Git is confined to the worktree. `run_git_in_worktree` (`sampler/worktree.rs`) requires `dir/.git`, sets `GIT_CEILING_DIRECTORIES` to the parent and refuses unless `--show-toplevel` is the directory itself; observe, tidy and the tree snapshot all go through that gate, removal refuses a non-toplevel directory, and a retired tree whose state is unknown counts as needing rescue. Left over: `git_stdout` and `git worktree remove` have no timeout, and the sampler's read-only HEAD reads are unconfined but run with `--no-optional-locks` and a 2 s kill.
- Force pushes lease on the observed head: `shape.rs` reads the remote head with `ls-remote` at replay time, refuses an empty one, and moves the local branch only after the push succeeds. `kit/pr.rs` still pushes agent branches with `--no-verify`.
- A failed notification fails the tick and reverts the sent-marker, so the next tick retries, and the daemon tick now reaches the forge.
- Merging is off by default. `merge_pr` refuses without `allow_merge`, and only `tacho land` with `TACHO_LAND_MERGE=1` or a config flag (default false) turns it on.

Blockers:

- A failed tick never records its decide time, so `decide_due` fires on every 2 s sampler pass instead of every 60 s. A forge outage now fails the tick, so an outage becomes a hot loop of gh calls with 15 s timeouts. It needs backoff.
- A recorded worktree path that is the main checkout passes `is_git_checkout`, and once idle it could be rescued and deleted. Only a live or held path protects it.

Tests, sandboxed with `TACHO_HOME` and `TACHO_RUNTIME` in a scratch directory and a watch on every git process the run started: build 1 min 35 s; 888 non-daemon tests and 84 daemon tests passed with none failed, at load ~150 on 36 CPUs. No git ran outside the scratch directory and the checkout, and the fleet root was unchanged afterwards. Three `tacho daemon` processes left by an earlier version's suite had kept running for two days and were stopped by hand.

Next review once a failed tick backs off and the main checkout can never be a removal target.

## The last stretch, from 31bd756

Everything an agent needs to take shunt from here to running the fleet, in order.

1. **Back off a failed tick.** Record the decide time on failure too, and wait 60 s, then 2, 4 and up to 15 minutes after each consecutive failure; reset on success. Test: a forge that fails every call makes the sampler run decide once per backoff step, not once per 2 s pass.
2. **Never remove the main checkout.** A recorded worktree path that is the repository's main checkout, or holds it, is refused by tidy and by `RemoveCheckout` whatever its state. Test: record the main checkout as a retired worktree and run tidy; it survives with its index and untracked files intact.
3. **Refuse a worker that has no worktree of its own.** On 25 Oct an nzfleet worker was started with the main checkout as its directory and edited it in place, on a commit a day old with no branch, beside other workers. Shunt starts every worker in its own worktree on a named branch off fresh main, and a worker whose directory resolves to the main checkout is stopped with a page. Test: start a worker whose directory is the main checkout; it never runs.
4. **Bound the residual git calls.** `git_stdout` and `git worktree remove` get the same kill timeout as the tree snapshot, and `kit/pr.rs` stops pushing agent branches with `--no-verify`.
5. **Prove parity for what was never reviewed.** Shunt already has provider `tell`, bisect, train compile and area selection. Write a parity test against these nzfleet behaviours before trusting it with them:
   - the train refuses its result when main or any member's head moves while checks run (`premerge.py`, `trainrun.py`);
   - screenshots are redrawn on the merged tree, only touched screens committed, a golden-list move fails (`shots.py`, `pixels.py`);
   - the owner hears every ready train as one merge command, and every stop, stall and red main, by mail with redaction (`owner.py`, `mail.py`, `redact.py`);
   - the stall watch counts child processes, commits and file changes (`progress.py`, `supervise.py`);
   - the runner pool is set up, pruned and kept warm from repository config (`setup.py`, `warm.py`, `janitor.py`);
   - the guards: no loosened limits, no vague waits, no time-based tests (`guard.py`);
   - migrations are named by the UTC second (`migrations.py`);
   - every temporary file goes under the fleet's tmp directory, never `/` (`tmpdirs.py`, `rootdisk.py`).
6. **Run beside nzfleet.** Shunt drives two real workers for a day while nzfleet keeps the rest, with `TACHO_LAND_MERGE` unset, and its decisions are diffed against nzfleet's for the same ticks. Any action nzfleet would not take is a bug to fix before step 7.
7. **Cut over one command at a time,** as "How to work" says: nzfleet's command becomes a thin call into shunt once its parity test passes.

Done means: the suite green five times in a row under load, steps 1 to 5 each with a test that fails without the fix, and a day of step 6 with no action nzfleet would not have taken.

## Where shunt stands at 8a6ea0b

Both blockers from 31bd756 are fixed, and so is most of step 4 of the last stretch.

- A failed decide tick now records its time and backs off 1, 2, 4, 8 and 16 times the refresh interval, resetting on success; a tick asked for by hand still runs at once (`sampler/mod.rs`, test in `sampler/tests.rs`).
- Tidy and `RemoveCheckout` remove only a linked worktree (`is_linked_worktree`), so neither the main checkout nor a standalone clone can be deleted, and removal goes through `git worktree remove` from the owning repository with a check that the directory is gone.
- `git_stdout` runs through the confined helper with a 30 s timeout, the sampler's HEAD and rev-list reads are confined, and agent branches are pushed without `--no-verify`.

Tests, sandboxed as before: build 3 min 27 s at load ~190; 888 non-daemon and 85 daemon tests passed with none failed; the fleet root unchanged and the checkout clean afterwards, and no daemon process left behind. Nineteen `tacho daemon` processes from earlier versions' test runs (23 Oct and the edf6662 run) were still alive and were stopped.

Still open: steps 3 (refuse a worker without its own worktree), 5 (parity tests), 6 (a day beside nzfleet) and five green runs in a row under load.

A test run of 8a6ea0b left a `yes` burner from the starve watch running for nine hours, reparented to init, at a full core (found 25 Oct at load 176). Before cutover, a starve test must reap its burner's process group in every exit path, and the suite must end with no `yes` whose environment carries `TACHO_STARVE=1`; add that check to the "no process left behind" assertion.

## Where shunt stands at 0aa4ecd

Step 3 is done at spawn: `fleet/session.rs` `spawn_worker_in_dir` refuses a worker whose directory is not its own linked worktree, holds the task and tells the owner before anything runs, and the test (a `touch` provider whose marker must never appear) fails without the check. Resume (`deliver_tell_action`) and the decide-time sweep (`main_checkout_workers`) check too but have no test. The backoff is capped at 15 times the refresh interval, with a test of the exact tick times.

Most of step 5 arrived as helpers that only their own tests call: `land/shots.rs` (`shots_fix_verdict`, `classify_moved_goldens`), `watch/guard.rs` (loosened limits, vague waits, time-based tests in a diff), `ops/kit.rs` (UTC migration names), `ops/config.rs` (`check_root_disk`, the runner tmp drop-in) and `verify/areas.rs` (costly-area and related-test selection). Each still has to be called from the path nzfleet uses it on, with a test through that path: shots on the merged train tree, guards in the pre-push and worker hooks, migrations from `kit`, the root-disk check in the tick, selection in the batch planner compared case by case with nzfleet's `areas.py`. Train head-move refusal, owner mail and redaction, the stall watch, and runner pool setup, prune and warm are not in this commit at all. The leaked starve burner is not addressed.

New in this commit and to settle before running beside nzfleet:

- `exec/mod.rs` `SuperviseAction::Escalate` now SIGTERMs the worker and holds the task on every escalation, ordinary stalls included. nzfleet escalates a stall by telling the owner, not by killing. Kill only a worker found in the main checkout, and signal its process group so its children go too.
- `resolves_to_main_checkout` treats any directory that directly contains a repository as the main checkout, which together with the kill above can stop a live worker under a holder directory. Compare against the fleet's recorded main checkout path instead.
- The worktree provisioning fallback runs from whatever checkout the current directory is in; it must refuse outside the fleet root.
- The step-3 test reaches `notify_owner_command`; tests must not reach a real mail hook.

Tests at 0aa4ecd, sandboxed as before at load about 60: build 72 s; 919 non-daemon and 88 daemon tests passed, none failed; the fleet root and the shunt checkout unchanged; no daemon and no `yes` burner left after this run. One clean run is not the five in a row under load that done requires, and the burner leak was seen on an earlier run, so its check belongs in the suite rather than in this script.

## Where shunt stands at 523dab5

The five open items from 0aa4ecd are settled in code, and most of step 5 is now called from real paths.

- Escalate kills only a worker found in the main checkout (SIGTERM to the verified pid, then its process group); an ordinary stall records a decision and tells the owner, with no kill and no hold (`exec/mod.rs`).
- `resolves_to_main_checkout` compares against the recorded `TACHO_REPO`, so a holder directory is no longer mistaken for the main checkout.
- The provisioning fallback runs only inside the fleet root (`is_within_fleet_root`); outside it the task is held and the start refused. With no explicit repo, the parent of the store home also counts as inside, which is wide.
- Tests reach the owner only through a `Hands::notify_owner` method, and in a test build `notify_owner_command` uses only `TACHO_TEST_NOTIFY_CMD`. Detecting a test binary in production code is a smell, but it protects. Direct calls remain in `fleet/land.rs` and `fleet/watch.rs`.
- The starve burner is reaped by a guard in the test and by `BurnersGuard::stop_all`, and `scripts/test-suite.sh` fails the run if a `yes` with `TACHO_STARVE=1` survives. That check is in the script, not in `cargo test`, and there is still no parent-death signal, so a SIGKILLed runner can leak one.

Wired now, each with tests through the path: shots on the merged train tree and in the batch (`stage_and_commit_merged_screenshots`; a golden move fails the batch), train head-move refusal (`batch_refs_moved`), costly-area selection in the batch planner, `TACHO_BASE` in proof checks, the guards in the hook, UTC migration names in `kit`, and the root-disk read in the tick. `tacho shots accept <run>` exists and refuses when the run's head is not HEAD. Two things to fix: `ShotRules::default()` hard-codes the nzela screenshot paths, area names and aliases in core, and every caller uses it with no config; and the root-disk action only records `prune_root_disk_volumes`, it prunes nothing.

Added in these commits and still called only by their own tests: `plan_actions_tidy`, `ci_proof_areas`, `affected_rust_targets`, the screen tag caps, `related_console_specs`, `related_package_tests_with_modules` and `DEFAULT_DEPLOY_CHECKS`. That means no data-loss risk yet. What they do matches nzfleet where I compared:

- `plan_actions_tidy` equals `actions-tidy.sh`: caches by last access, artifacts by age, the newest 3 per name kept, older ones removed once two hours settled, with release-image, ci-proof and verified-image-digest exempt. It is a pure planner with no delete hand.
- `ci_proof_areas` equals `trainpr.py`: every area until main's CI has passed at or after the proof sha.
- The runner handback in `ci_runner.rs` has the same design as `plan.py`: a missing path is not a refusal, any other chown refusal fails an otherwise green job. Its test only checks that the script text contains the pieces; it does not run it.
- `affected_rust_targets` under-selects against `changed_tests.py::_rust_affected`: a change under `fuzz/` selects nothing where Python selects everything. It also reads Cargo.toml with regexes where Python parses it. Fix before it is called.

No new data-loss or live-worker-killing blocker, and no time-based test seen. Not run beside nzfleet yet, and parity for the stall watch, mail and redaction, and the runner pool was not examined.

Tests at 523dab5, sandboxed as before, with the 1.98 toolchain: build 2 min 44 s, load falling from about 90 to 37; 954 non-daemon tests and 78 daemon tests passed, 3 daemon tests failed. Two failures are environmental to this machine: `test_holder_directory_worker_is_not_mistaken_for_main_checkout` and `test_ordinary_stall_escalation_notifies_owner_without_killing_worker` put their fixtures under a temp directory that sits inside another git repository, and with `TACHO_REPO` unset the fallback treats any directory under a repository with a `.git` directory as the main checkout. The tests should set `TACHO_REPO` or make their own root, and the fallback should not trust an enclosing repository it was not told about. The third, `test_cgroup_integration_accounting`, failed to find its daemonized burner in a process slot at load above 60. The ordinary-stall test therefore did not get to prove the no-kill path here. The fleet root was unchanged, the checkouts clean, and no daemon or `yes` burner was left behind.

Verdict: the 0aa4ecd blockers are fixed and step 5 is mostly wired, but the suite is red here on three tests and the tidy, CI-proof and incremental-target helpers are still uncalled, so not ready to run beside nzfleet.
