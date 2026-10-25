---
name: nzfleet
description: Use when working on nzela-org/nzela PRs, CI, merges, deploys or the agent work queue on this machine. nzfleet moves PRs from draft to deployed; start with `nzfleet next`.
---

nzfleet runs the nzela pipeline on this machine: it batches PRs, finds the one that breaks a batch, tells the owner what to merge (issue #811), deploys the test environment, and starts agents for queued work.

1. Run `nzfleet next`. It prints the one thing to do now. Do it.
2. Run `nzfleet tick` after you push, so it notices straight away.
3. Start every session with `nzfleet status --json --section needs`: what needs the owner and what needs an agent, each with the command that fixes it. `nzfleet status` shows everything (from the snapshot the daemon writes each tick; `--section` picks one); `nzfleet --help` and `nzfleet <command> --help` explain each command.
4. To queue work for an agent: `nzfleet todo <key> <brief file>`.

When you work as an agent, run `nzfleet note <key> "<phase>: <one line>"` (phase: read, write, test, fix, push) at each change of phase; `nzfleet status --section progress` (alias `nzfleet progress`) shows how every agent and chain is moving. A run that ends without its final report is resumed in its worktree; do not leave work uncommitted for long. A starvation test runs under `nzfleet starve -- <cmd>`, never a free-running `yes` loop; `stress` and `stress-ng` are load generation too, and a held task that is finished or superseded is retired with `nzfleet retire <key> --why "..."`.

Push PR branches as their own plain commits on main or the PR they stack on. Never merge, never apply Terraform. Anything that needs the owner goes on issue #695.

A fresh machine (`nzfleet setup` is safe to repeat):

1. Clone the repository with an `upstream` remote for nzela-org/nzela, then run `tools/nzfleet/bin/nzfleet setup`.
2. It writes `~/.config/nzfleet/env` from `bin/env.template` the first time. Fill in what is commented out; no secrets go in it, only paths and names.
3. It installs and enables the systemd user units, links `nzfleet`, `agent-db` and `gradle-slot` into `~/.local/bin`, and makes the daemon's worktree of main.
4. It lists what is missing: docker, rustup 1.98, node 22, the Android SDK, gcloud, `gh auth login`, the phones' adb host. Fix them and run it again until it says nothing is missing.
5. As root, once: install `tools/nzfleet/runner/runner-prune.sh` as `/usr/local/sbin/nzela-runner-prune`, add the one crontab line setup prints, and the sudoers rule for `nzela-runner-prune check`. Setup never touches root's crontab.

The daemon tidies the machine every 15 minutes (idle build output, Postgres containers, finished worktrees, old Robolectric unpacks; nothing a running agent, batch or process is using). `nzfleet tidy --dry-run` shows what it would remove, including a build cache past its size (the batches' warm slots under `$NZFLEET_HOME/warm`, sccache, npm and registry caches). A batch builds in a warm slot: its log says `warm: <slot>`, and each area's `stats:` line gives wall time, CPU minutes, sccache hits and misses and the tests that passed. A cache gone bad is cleared with `rm -rf` of that directory (tools/nzfleet/README.md, Warm slots). `nzfleet batch <pr>...` detaches and uses the machine's env, so it behaves the same from any shell; `--foreground` waits for the verdict.
