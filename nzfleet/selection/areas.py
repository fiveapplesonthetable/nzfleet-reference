"""The checks a set of changed paths calls for, as a pure function of the paths and the tier.

`change` is what one pull request touched, and is what the pre-push hook and a batch of one run. `train`
is the merged tree of several: the union of what its members touched, held to the cheap guards whether
or not anything moved, and narrower about the two expensive areas the members' own checks already ran.
It always runs the server's checks (compile, contract, migrations, Rust tests, fuzz, e2e), and the
Android suite and the console browser suite only for what it reads. Neither tier selects formal, bench
or a full screenshot sweep: main runs `full`, which is everything, after the merge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALL = ("secrets", "rust", "fuzz", "e2e", "console", "site", "android", "image", "deploy", "fleet", "compat", "contract")
TIERS = ("change", "train")

# What each client reads of the server, besides its own tree: the files the wire types are written
# from and the routes it calls. A test holds these to what the train planner reads.
ANDROID_READS = (r"^crates/nzela-wire/|^frontend/android/|^crates/nzela-api/src/|^crates/nzela-web/src/code\.rs$"
                 r"|^scripts/(contract\.sh|typeshare\.toml)$")
CONSOLE_READS = (r"^crates/nzela-wire/|^frontend/admin/|^crates/(nzela-operator-[a-z]+|nzela-web)/src/"
                 r"|^scripts/(contract\.sh|typeshare\.toml)$")

# These files decide what every area does, so a change to one is a change to all. An area's own
# checks are in scripts/areas/<area>.sh and select only that area.
EVERYTHING = re.compile(
    r"^scripts/checks\.sh$|^tools/nzfleet/nzfleet/selection/(areas|since|changed_tests|files)\.py$"
    r"|^\.github/workflows/ci\.yaml$|^\.cargo/|^rust-toolchain(\.toml)?$|^rustfmt\.toml$|^clippy\.toml$"
    r"|^Makefile$|^\.gitmodules$|^tools/nzfleet/nzfleet/areas\.py$")

E2E_INPUTS = (r"^sim/(e2e|bench|smoke)\.toml$|^sim/(e2e|bench)\.sh$|^sim/lib/|^tools/nzfleet/devloop/"
              r"|^deploy/seed-dev\.sql$|^crates/[^/]*/migrations/|^crates/nzela-api/src/|^crates/nzela-wire/"
              r"|^frontend/android/app/src/main/java/app/nzela/data/remote/|^Cargo\.lock$")

CONTRACT_INPUTS = (r"^crates/nzela-wire/|^crates/nzela(-platform|-api|-verification|-operator-[a-z]+)?/src/"
                   r"|^scripts/(contract\.sh|typeshare\.toml)$|^frontend/admin/src/api/generated\.ts$"
                   r"|^frontend/android/core/data/src/main/java/app/nzela/data/remote/generated/")

DEPLOY_INPUTS = (r"^deploy/Dockerfile$|^deploy/compose.*\.yaml$"
                 r"|^tools/nzfleet/runner/|^sim/(bench-report|perf|test_perf)|^tools/nzfleet/backup/(backup|offsite-backup)|^tools/nzfleet/release/")


# What a pull request must build to catch what main would otherwise catch. The release image is built from
# the workspace manifests, the Dockerfile and what the deploy tree ships; the release APK from the build
# scripts, the manifest and the shrinker rules; the browser suite from everything under the console except
# the pictures and prose it only holds. A train carries all three, since main reuses its image.
IMAGE_INPUTS = re.compile(
    r"^deploy/|^\.dockerignore$|^Cargo\.(toml|lock)$|^crates/[^/]+/Cargo\.toml$|^\.sqlx/|^rust-toolchain(\.toml)?$"
    r"|^\.cargo/|^tools/nzfleet/backup/(backup|restore-drill|offsite-restore-check)|^tools/nzfleet/release/(push-image|reuse-image|hand-over|build-release-binary|build-images)")
ANDROID_RELEASE_INPUTS = re.compile(
    r"^frontend/android/(.*\.gradle(\.kts)?|gradle\.properties|gradle/.*|.*proguard.*|.*consumer-rules\.pro"
    r"|.*/AndroidManifest\.xml|.*/src/release/.*|scripts/(apk-|release-version).*)$"
    r"|^scripts/areas/android\.sh$|^\.github/workflows/ci\.yaml$")
CONSOLE_BROWSER_IDLE = re.compile(r"^frontend/admin/(screenshots/|docs/|screenshots\.golden$)|^frontend/admin/[^/]*\.md$")


@dataclass(frozen=True)
class Rule:
    """Paths matching `pattern` call for `areas`, in `tiers` only when given."""

    pattern: re.Pattern
    areas: tuple
    tiers: tuple = TIERS


def _rule(pattern: str, *areas: str, tiers: tuple = TIERS) -> Rule:
    return Rule(re.compile(pattern), areas, tiers)


RULES = (
    _rule(r"^scripts/areas/", "deploy", tiers=("change",)),
    *(_rule(rf"^scripts/areas/{area}\.sh$", area) for area in ALL),
    _rule(r"^scripts/areas/compile\.sh$", "compile", tiers=("train",)),
    _rule(r"^crates/|^Cargo\.(toml|lock)$|^\.sqlx/|^\.config/nextest\.toml$|^fuzz/", "rust", "fuzz"),
    # e2e drives the whole machine through its API, so it runs when the API surface, the app's network
    # layer or the schema moved. A train runs it always, and not for any other crate change.
    _rule(r"^crates/|^Cargo\.(toml|lock)$|^\.sqlx/", "e2e", tiers=("change",)),
    _rule(E2E_INPUTS, "e2e"),
    _rule(r"^frontend/admin/", "console"),
    _rule(r"^frontend/website/", "site"),
    _rule(r"^frontend/android/", "android"),
    # The release image is built by the pull request run, or on main when none covers the tree.
    _rule(r"^tools/nzfleet/backup/(backup|restore-drill|offsite-restore-check)|^tools/nzfleet/release/(hand-over|build-release-binary)", "image"),
    _rule(r"^deploy/Dockerfile$|^\.dockerignore$", "image", tiers=("change",)),
    _rule(r"^tools/nzfleet/runner/install-", "rust", "fuzz"),
    _rule(DEPLOY_INPUTS, "deploy"),
    _rule(r"^deploy/terraform/|^\.githooks/|^tools/nzfleet/deploycheck/", "deploy"),
    _rule(r"^scripts/(no-test-sleeps|no-counted-polls)", "deploy", tiers=("change",)),
    # Every other script belongs to the area whose checks call it; a shared one names each caller.
    _rule(r"^scripts/(android-|local-android-|no-android-|no-shared-test-state|device\.sh|emulator)", "android"),
    _rule(r"^scripts/(console-capture-env|no-capture-network-idle|no-vague-console-waits)", "console"),
    _rule(r"^scripts/(core-boundary|coverage_report|crate-|one-clock|restore-mtimes|rust-|rustc-wrapper|warm-target)", "rust"),
    _rule(r"^scripts/(emulator|e2e-build|no-wall-clock-waits|no-rust-test-deadlines)", "e2e"),
    _rule(r"^scripts/(console-capture-env-test|no-wall-clock-waits|no-rust-test-deadlines|step-cap-test|suite-caps)", "deploy", tiers=("change",)),
    _rule(r"^scripts/(query-validation-cache|record-wire-fixtures)", "compile", tiers=("train",)),
    _rule(r"^scripts/(query-validation-cache|record-wire-fixtures)", "rust"),
    _rule(r"^scripts/coverage-floors$|^scripts/coverage_report", "deploy", tiers=("change",)),
    # Every workflow: which machine a job may ask for is asserted across all of them.
    _rule(r"^\.github/workflows/", "deploy", tiers=("change",)),
    _rule(r"^docs/book/|^tools/nzfleet/book/", "deploy", tiers=("change",)),
    _rule(r"^tools/nzfleet/", "fleet", tiers=("change",)),
    _rule(CONTRACT_INPUTS, "contract"),
    _rule(r"^crates/[^/]*/migrations/|^scripts/schema-compat", "compat"),
)

# Paths that call for no check beyond `secrets`, which every push runs.
NO_AREA = re.compile(
    r"^(docs/|journeys/|formal/|perf/|\.night/|\.claude/|sim/)|^(README\.md|README|LICENSE|CLAUDE\.md|AGENTS\.md"
    r"|\.gitignore|\.editorconfig|\.gitattributes)$|^\.github/(?!workflows/)")

# Paths whose area a rule above names, or that need none. A path outside both is unplaced.
PLACED = re.compile(
    r"^(crates|fuzz|frontend/admin|frontend/android|frontend/website|deploy|tools/nzfleet|\.github|\.sqlx|\.githooks"
    r"|\.cargo|\.config)/|^(Cargo\.toml|Cargo\.lock|rust-toolchain(\.toml)?|rustfmt\.toml|clippy\.toml|Makefile"
    r"|\.gitmodules|\.dockerignore)$"
    r"|^scripts/("
    r"contract\.sh|typeshare\.toml|schema-compat.*|areas/.*\.sh)$")


def select(paths: list, tier: str) -> tuple:
    """The areas `paths` call for in `tier`, and the paths no rule could place."""
    if tier in ("all", "full"):
        return ALL, ()
    if tier not in TIERS:
        raise ValueError(f"no such tier: {tier}")
    paths = [p for p in paths if p]

    def touched(pattern) -> bool:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        return any(compiled.search(p) for p in paths)

    if any(EVERYTHING.search(p) for p in paths):
        return _finish(ALL, tier, touched), ()

    # Every push, whatever it touches: a credential can be committed in any file.
    areas = ["secrets"]
    for rule in RULES:
        if tier in rule.tiers and touched(rule.pattern):
            areas.extend(rule.areas)
    if tier == "train":
        areas.extend(("rust", "fuzz", "e2e", "contract", "compile"))
        for area, reads in (("android", ANDROID_READS), ("console", CONSOLE_READS)):
            if touched(reads):
                areas.append(area)

    unplaced = tuple(p for p in paths if not _placed(p))
    # A script no rule places has callers nobody listed, so it is every area's change. Any other path no
    # rule knows can introduce a shared build input; deploy holds the inventory test that refuses an
    # unplaced file, so the push that adds one learns of it there rather than in every area.
    if any(p.startswith("scripts/") for p in unplaced):
        return _finish(ALL, tier, touched), unplaced
    if unplaced:
        areas.append("deploy")
    return _finish(areas, tier, touched), unplaced


def _placed(path: str) -> bool:
    return bool(PLACED.search(path) or NO_AREA.search(path) or EVERYTHING.search(path)
                or any(rule.pattern.search(path) for rule in RULES))


def _finish(areas, tier: str, touched) -> tuple:
    """Dedupe in order; a train asks a client suite only for what it reads, whatever else it touched."""
    out = []
    for area in (*areas, "compile") if tier == "train" else areas:
        if tier == "train":
            if area == "android" and not touched(ANDROID_READS):
                continue
            if area == "console" and not touched(CONSOLE_READS):
                continue
        if area not in out:
            out.append(area)
    return tuple(out)
