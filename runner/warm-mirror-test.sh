#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

git init -q -b main "$tmp/origin"
for n in 1 2 3; do git -C "$tmp/origin" commit -q --allow-empty -m "c$n"; done
git clone -q "$tmp/origin" "$tmp/first"
(cd "$tmp/first" && "$root/tools/nzfleet/runner/warm-mirror.sh" keep "$tmp/mirror")

# A fresh workspace that borrows the mirror holds no object of its own for
# history the mirror has, and still reaches a commit the mirror lacks.
git -C "$tmp/origin" commit -q --allow-empty -m c4
new=$(git -C "$tmp/origin" rev-parse HEAD)
git init -q "$tmp/job"
mkdir -p "$tmp/job/.git/objects/info"
echo "$tmp/mirror/objects" > "$tmp/job/.git/objects/info/alternates"
git -C "$tmp/job" fetch -q --no-tags "$tmp/origin" "+refs/heads/*:refs/remotes/origin/*"
git -C "$tmp/job" checkout -q --detach "$new"
git -C "$tmp/job" merge-base --is-ancestor "$(git -C "$tmp/origin" rev-parse HEAD~3)" "$new"
loose=$(git -C "$tmp/job" count-objects -v | sed -n 's/^count: //p')
packed=$(git -C "$tmp/job" count-objects -v | sed -n 's/^in-pack: //p')
[ $((loose + packed)) -le 2 ]

# The mirror takes the new history from the job, so the next job borrows it.
(cd "$tmp/job" && "$root/tools/nzfleet/runner/warm-mirror.sh" keep "$tmp/mirror")
git -C "$tmp/mirror" cat-file -e "$new"
printf '%s\n' 'a job borrowing the mirror fetches only what the mirror lacks, and hands that back'

# The runner's job-started hook keeps the mirror on main, with main stored as a
# branch so a borrowing checkout has a "have" line and downloads nothing it holds.
git clone -q --bare "$tmp/origin" "$tmp/server/nzela.git"
hook() {
  NZELA_GIT_MIRROR="$tmp/runner/git-mirror/repo.git" GITHUB_SERVER_URL="file://$tmp/server" GITHUB_REPOSITORY=nzela \
    GITHUB_WORKSPACE='' "$root/tools/nzfleet/runner/runner-job-started.sh" 2>/dev/null
}
hook
git -C "$tmp/runner/git-mirror/repo.git" cat-file -e "$new"
git -C "$tmp/runner/git-mirror/repo.git" rev-parse --verify -q refs/heads/main >/dev/null

git -C "$tmp/origin" commit -q --allow-empty -m c5
git -C "$tmp/origin" push -q "$tmp/server/nzela.git" main
newer=$(git -C "$tmp/origin" rev-parse HEAD)
hook
git -C "$tmp/runner/git-mirror/repo.git" cat-file -e "$newer"

# A server that cannot be reached leaves the mirror as it was and does not fail the job.
GITHUB_SERVER_URL="file://$tmp/nowhere" NZELA_GIT_MIRROR="$tmp/runner/git-mirror/repo.git" GITHUB_REPOSITORY=nzela \
  GITHUB_WORKSPACE='' "$root/tools/nzfleet/runner/runner-job-started.sh" 2>/dev/null
git -C "$tmp/runner/git-mirror/repo.git" cat-file -e "$newer"

# A checkout borrowing it asks the server only for what the mirror lacks.
git -C "$tmp/origin" commit -q --allow-empty -m c6
git -C "$tmp/origin" push -q "$tmp/server/nzela.git" main
git init -q "$tmp/job2"
mkdir -p "$tmp/job2/.git/objects/info"
echo "$tmp/runner/git-mirror/repo.git/objects" > "$tmp/job2/.git/objects/info/alternates"
git -C "$tmp/job2" fetch -q --no-tags "file://$tmp/server/nzela.git" '+refs/heads/main:refs/remotes/origin/main'
loose=$(git -C "$tmp/job2" count-objects -v | sed -n 's/^count: //p')
packed=$(git -C "$tmp/job2" count-objects -v | sed -n 's/^in-pack: //p')
[ $((loose + packed)) -le 2 ]
printf '%s\n' 'the runner hook keeps main in the mirror and survives a server it cannot reach'
