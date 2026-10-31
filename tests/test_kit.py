import os
import tempfile
import unittest

from nzfleet import plan, prove
from nzfleet.plan import Check

TRACKED = [
    "crates/nzela/Cargo.toml", "crates/nzela/src/lib.rs", "crates/nzela/src/main.rs", "crates/nzela/src/pricing.rs",
    "crates/nzela/src/rates/mod.rs", "crates/nzela/src/rates/feed.rs", "crates/nzela/tests/properties.rs",
    "crates/nzela-core/Cargo.toml", "crates/nzela-core/src/lib.rs", "crates/nzela-core/src/money.rs",
    "frontend/admin/e2e/walk.spec.ts", "frontend/admin/e2e/lists.spec.ts", "frontend/admin/e2e/operator.ts",
    "frontend/admin/e2e/test.ts", "frontend/admin/e2e/settled.ts",
    "frontend/admin/src/App.tsx", "frontend/admin/tests/money-format.test.mjs",
    "frontend/android/app/src/main/java/app/nzela/ui/SendScreen.kt",
    "frontend/android/app/src/test/java/app/nzela/ui/SendScreenTest.kt",
    "frontend/android/app/src/sharedTest/java/app/nzela/shared/TheAppStartsTest.kt",
    "docs/ci-runners.md", "scripts/checks.sh",
]


def names(files):
    return [c.name for c in plan.proof_plan(files, TRACKED, "BASE")]


def find(files, name):
    return next(c for c in plan.proof_plan(files, TRACKED, "BASE") if c.name.startswith(name))


class Mapping(unittest.TestCase):
    def test_a_console_spec_change_checks_the_recorded_source_coverage(self):
        self.assertIn("console source coverage", names(["frontend/admin/e2e/walk.spec.ts"]))

    def test_a_generated_source_coverage_map_does_not_select_every_browser_spec(self):
        checks = plan.proof_plan(["frontend/admin/e2e/source-coverage.json"], TRACKED, "BASE")
        self.assertEqual([c.name for c in checks], ["whitespace", "secrets", "console source coverage"])
    def test_a_screenshot_runner_test_selects_its_console_test_without_full_presubmit(self):
        checks = plan.proof_plan(["frontend/admin/scripts/tests/shot-run.test.mjs"], TRACKED, "BASE")
        self.assertEqual([c.name for c in checks], ["whitespace", "secrets", "console script tests"])
        self.assertEqual(checks[-1].cmd, ("node", "--test", "scripts/tests/shot-run.test.mjs"))

    def test_an_unmapped_console_script_stays_inside_the_complete_console_checks(self):
        checks = plan.proof_plan(["frontend/admin/scripts/shot-run.mjs"], TRACKED, "BASE")
        self.assertNotIn("conservative checks", [c.name for c in checks])
        self.assertIn("console screenshots", [c.name for c in checks])
        self.assertTrue(any(c.name.startswith("console specs:") for c in checks))
        self.assertFalse(any(c.cmd[0] in ("cargo", "./gradlew") for c in checks))
    def test_android_recording_does_not_rerun_the_same_unit_task_with_a_different_filter(self):
        checks = plan.proof_plan(["frontend/android/app/src/main/java/app/nzela/ui/SendScreen.kt"], TRACKED, "BASE")
        gradle = [c for c in checks if c.slot]
        self.assertEqual(len(gradle), 1)
        self.assertIn("testDebugUnitTest", gradle[0].cmd)
        self.assertIn("-Proborazzi.test.record=true", gradle[0].cmd)

    def test_recording_keeps_the_pure_model_suite_when_shared_build_inputs_change(self):
        checks = plan.proof_plan(["frontend/android/gradle.properties", "frontend/android/app/src/main/res/values/strings.xml"], TRACKED, "BASE")
        gradle = [c for c in checks if c.slot]
        self.assertEqual(len(gradle), 1)
        self.assertIn(":core:model:test", gradle[0].cmd)
    def test_a_website_edit_runs_its_route_and_form_checks_without_other_frontends(self):
        checks = plan.proof_plan(["frontend/website/site.js"], TRACKED, "BASE")
        self.assertEqual([check.name for check in checks], ["whitespace", "secrets", "site"])
        self.assertEqual(checks[-1].cmd, ("node", "check.mjs"))
        self.assertEqual(checks[-1].cwd, "frontend/website")

    def test_query_metadata_keeps_the_proof_of_the_crate_whose_queries_changed(self):
        files = [".sqlx/query-abcd.json", "crates/nzela-core/src/money.rs"]
        checks = plan.proof_plan(files, TRACKED, "BASE")
        self.assertNotIn("conservative checks", [check.name for check in checks])
        self.assertEqual([check.cmd for check in checks if check.name.startswith("rust tests")],
                         [("cargo", "+1.98", "test", "--locked", "-p", "nzela-core", "--lib", "--", "money::")])

    def test_query_metadata_without_a_query_source_change_checks_the_workspace(self):
        checks = plan.proof_plan([".sqlx/query-abcd.json"], TRACKED, "BASE")
        self.assertNotIn("conservative checks", [check.name for check in checks])
        self.assertIn(("cargo", "+1.98", "clippy", "--locked", "--workspace", "--all-targets"), [check.cmd for check in checks])

    def test_a_capture_engine_change_runs_capture_checks_without_other_product_suites(self):
        files = ["frontend/admin/scripts/capture.mjs"]
        checks = plan.proof_plan(files, TRACKED + files, "BASE")
        self.assertEqual([check.name for check in checks],
                         ["whitespace", "secrets", "console capture tests", "console build", "console screenshots"])

    def test_a_capture_regression_test_does_not_redraw_the_catalogue(self):
        files = ["frontend/admin/scripts/tests/capture-browser.test.mjs"]
        checks = plan.proof_plan(files, TRACKED + files, "BASE")
        self.assertEqual([check.name for check in checks], ["whitespace", "secrets", "console capture tests"])
        self.assertEqual(checks[-1].cmd, ("npm", "run", "test:capture"))
    def test_browser_worker_policy_has_a_console_proof(self):
        checks = names(["frontend/admin/playwright.workers.ts"])
        self.assertNotIn("conservative checks", checks)
        self.assertIn("console unit tests", checks)

    def test_a_documentation_change_still_runs_the_secret_scan(self):
        self.assertEqual(names(["docs/ci-runners.md"]), ["whitespace", "secrets"])

    def test_a_book_change_runs_the_secret_scan(self):
        checks = plan.proof_plan(["docs/book/04-flows.md"], TRACKED, "BASE")
        self.assertEqual([c.name for c in checks], ["whitespace", "secrets"])

    def test_an_unmapped_crate_change_falls_back_to_all_checks(self):
        check = find(["crates/scratch/src/a.rs"], "conservative checks")
        self.assertEqual(check.cmd, ("scripts/checks.sh", "all"))
        self.assertEqual(check.needs, "db")

    def test_workflow_shell_checks_and_mapping_references_have_focused_proofs(self):
        files = ["tools/nzfleet/deploycheck/workflow-shells-test.sh", "tools/nzfleet/deploycheck/workflow-shells-diagnostics-test.sh",
                 "formal/tla/MAPPING.md"]
        checks = plan.proof_plan(files, TRACKED + files, "BASE")
        commands = [c.cmd for c in checks]
        self.assertNotIn(("scripts/checks.sh", "all"), commands)
        self.assertIn(("tools/nzfleet/deploycheck/workflow-shells-test.sh",), commands)
        self.assertIn(("tools/nzfleet/deploycheck/workflow-shells-diagnostics-test.sh",), commands)
        self.assertIn(("python3", "tools/nzfleet/deploycheck/tla-mapping-refs.py"), commands)

    def test_a_shell_checker_change_also_runs_its_diagnostic_regressions(self):
        checks = plan.proof_plan(["tools/nzfleet/deploycheck/workflow-shells-test.sh"], TRACKED, "BASE")
        self.assertIn(("tools/nzfleet/deploycheck/workflow-shells-diagnostics-test.sh",), [c.cmd for c in checks])

    def test_the_deploy_runner_is_proved_by_its_area(self):
        checks = plan.proof_plan(["scripts/areas/deploy.sh"], TRACKED, "BASE")
        self.assertIn(("scripts/checks.sh", "deploy"), [c.cmd for c in checks])
        self.assertNotIn(("scripts/checks.sh", "all"), [c.cmd for c in checks])

    def test_the_nightly_money_path_change_runs_its_harness_regression(self):
        files = [".github/workflows/fuzz.yaml", "fuzz/money-paths.sh", "fuzz/nightly-test.sh"]
        commands = [check.cmd for check in plan.proof_plan(files, TRACKED + files, "BASE")]
        self.assertIn(("bash", "fuzz/nightly-test.sh"), commands)
        self.assertNotIn(("scripts/checks.sh", "all"), commands)

    def test_proof_planner_changes_run_the_planner_test_modules(self):
        files = ["tools/nzfleet/nzfleet/plan.py", "tools/nzfleet/tests/test_kit.py"]
        check = find(files, "nzfleet tests")
        self.assertEqual(check.cmd, ("python3", "-m", "unittest", "-q", "tests.test_plan", "tests.test_kit", "tests.test_shells"))

    def test_owned_check_runners_do_not_trigger_unrelated_product_suites(self):
        for area in ("android", "console"):
            path = f"scripts/areas/{area}.sh"
            checks = plan.proof_plan([path], TRACKED + [path], "BASE")
            commands = [check.cmd for check in checks]
            self.assertNotIn(("scripts/checks.sh", "all"), commands)
            self.assertIn(("scripts/checks.sh", area), commands)

    def test_workflows_and_android_inventory_keep_their_domain_proofs(self):
        files = [".github/workflows/console-spec-map.yaml", ".github/workflows/perf.yaml",
                 "scripts/android-catalogue.py", "scripts/android-catalogue-test.py",
                 "frontend/admin/README.md"]
        checks = plan.proof_plan(files, TRACKED + files, "BASE")
        commands = [check.cmd for check in checks]
        self.assertNotIn(("scripts/checks.sh", "all"), commands)
        self.assertIn(("python3", "scripts/android-catalogue-test.py"), commands)
        self.assertIn(("python3", "scripts/android-catalogue.py", "frontend/android/docs/screenshots"), commands)
        self.assertIn(("tools/nzfleet/runner/runner-trust-test.sh",), commands)
        self.assertIn(("tools/nzfleet/deploycheck/workflow-shells-test.sh",), commands)
        self.assertFalse(any(check.cmd[0] in ("cargo", "./gradlew") for check in checks))

    def test_owned_frontend_runners_use_independent_shunt_resource_lanes(self):
        from nzfleet.prove import shunt_plan

        files = ["scripts/areas/android.sh", "scripts/areas/console.sh"]
        checks = plan.proof_plan(files, TRACKED + files, "BASE")
        projected = shunt_plan(checks, "/checkout", {}, {}, "BASE", "/proof")
        areas = {check["name"]: check["area"] for check in projected}
        self.assertEqual(areas["android check runner"], "android")
        self.assertEqual(areas["console check runner"], "console")
        self.assertTrue(find(files, "android check runner").slot)

    def test_a_shared_check_script_falls_back_to_all_checks(self):
        check = find(["scripts/checks.sh"], "conservative checks")
        self.assertEqual(check.cmd, ("scripts/checks.sh", "all"))
        self.assertEqual(find(["scripts/checks.sh"], "shell syntax").cmd[-1], "scripts/checks.sh")

    def test_an_android_resource_change_refreshes_registered_screenshots(self):
        self.assertIn("android screenshots", names(["frontend/android/app/src/main/res/values/strings.xml"]))

    def test_a_changed_module_is_tested_by_its_own_path_and_nothing_wider(self):
        check = find(["crates/nzela/src/pricing.rs"], "rust tests")
        self.assertEqual(check.cmd, ("cargo", "+1.98", "test", "--locked", "-p", "nzela", "--lib", "--", "pricing::"))

    def test_a_nested_module_keeps_its_path_and_mod_rs_names_the_directory(self):
        check = find(["crates/nzela/src/rates/feed.rs", "crates/nzela/src/rates/mod.rs"], "rust tests")
        self.assertEqual(check.cmd[-2:], ("rates::", "rates::feed::"))

    def test_the_library_root_runs_the_whole_crate(self):
        check = find(["crates/nzela-core/src/lib.rs"], "rust tests")
        self.assertEqual(check.cmd, ("cargo", "+1.98", "test", "--locked", "-p", "nzela-core"))

    def test_a_migration_runs_the_whole_crate_because_the_queries_are_checked_against_it(self):
        check = find(["crates/nzela/migrations/20261001000000_x.sql"], "rust tests")
        self.assertEqual(check.cmd[-2:], ("-p", "nzela"))

    def test_an_integration_test_file_runs_as_that_test_target(self):
        check = find(["crates/nzela/tests/properties.rs"], "rust tests")
        self.assertEqual(check.cmd[-2:], ("--test", "properties"))

    def test_rust_always_gets_fmt_and_clippy_on_the_touched_crates_only(self):
        got = plan.proof_plan(["crates/nzela/src/pricing.rs", "crates/nzela-core/src/money.rs"], TRACKED, "BASE")
        clippy = next(c for c in got if c.name == "rust clippy")
        self.assertEqual(clippy.cmd, ("cargo", "+1.98", "clippy", "--locked", "-p", "nzela", "-p", "nzela-core", "--all-targets"))
        self.assertIn("rust fmt", [c.name for c in got])

    def test_rust_lock_and_toolchain_changes_test_the_workspace(self):
        for path in ("Cargo.lock", "rust-toolchain.toml"):
            check = find([path], "rust tests")
            self.assertIn("--workspace", check.cmd)

    def test_rust_checks_that_compile_need_the_database(self):
        got = plan.proof_plan(["crates/nzela/src/pricing.rs"], TRACKED, "BASE")
        self.assertTrue(all(c.needs == "db" for c in got if c.name.startswith(("rust clippy", "rust tests"))))

    def test_a_console_source_change_is_typechecked_unit_tested_and_drawn_again(self):
        self.assertEqual(names(["frontend/admin/src/App.tsx"]),
                         ["whitespace", "secrets", "console typecheck", "console unit tests", "console source coverage", "console build", "console screenshots"])

    def test_a_console_lockfile_change_also_builds_the_locked_dependencies(self):
        self.assertIn("console build", names(["frontend/admin/package-lock.json"]))

    def test_a_touched_spec_runs_alone_against_the_preview(self):
        check = find(["frontend/admin/e2e/walk.spec.ts"], "console specs")
        self.assertEqual(check.cmd, ("npx", "playwright", "test", "e2e/walk.spec.ts"))
        self.assertEqual(check.needs, "preview")

    def test_a_shared_spec_helper_runs_every_spec(self):
        for helper in ("operator.ts", "test.ts", "settled.ts"):
            with self.subTest(helper=helper):
                checks = plan.proof_plan([f"frontend/admin/e2e/{helper}"], TRACKED, "BASE")
                check = next(c for c in checks if c.name.startswith("console specs"))
                self.assertEqual(check.cmd[-2:], ("e2e/lists.spec.ts", "e2e/walk.spec.ts"))
                self.assertEqual(check.needs, "preview")

    def test_a_spec_change_does_not_redraw_screenshots(self):
        self.assertNotIn("console screenshots", names(["frontend/admin/e2e/walk.spec.ts"]))

    def test_a_screenshot_alone_proves_nothing_more(self):
        self.assertEqual(names(["frontend/admin/screenshots/01-a.png"]), ["whitespace", "secrets"])

    def test_a_changed_android_test_class_runs_under_the_slot(self):
        check = find(["frontend/android/app/src/test/java/app/nzela/ui/SendScreenTest.kt"], "android tests")
        self.assertEqual(check.cmd[-3:], (":app:testDebugUnitTest", "--tests", "app.nzela.ui.SendScreenTest"))
        self.assertTrue(check.slot)

    def test_the_recording_suite_covers_a_changed_android_source_without_a_second_unit_run(self):
        check = find(["frontend/android/app/src/main/java/app/nzela/ui/SendScreen.kt"], "android screenshots")
        self.assertIn("testDebugUnitTest", check.cmd)
        self.assertNotIn("--tests", check.cmd)

    def test_an_android_flow_test_edit_does_not_record_screenshots(self):
        self.assertNotIn("android screenshots", names(["frontend/android/app/src/test/java/app/nzela/flow/HomeFlowTest.kt"]))

    def test_an_android_capture_catalogue_edit_still_records_screenshots(self):
        self.assertIn("android screenshots", names(["frontend/android/app/src/test/java/app/nzela/screenshot/catalogue/TabStates.kt"]))

    def test_android_pixels_are_recorded_again_when_a_screen_changes(self):
        self.assertIn("android screenshots", names(["frontend/android/app/src/main/java/app/nzela/ui/SendScreen.kt"]))

    def test_android_without_a_matching_test_is_at_least_compiled(self):
        self.assertEqual(names(["frontend/android/gradle.properties"]),
                         ["whitespace", "secrets", "android tests: shared build inputs"])

    def test_an_android_module_build_change_runs_the_complete_unit_suite(self):
        check = find(["frontend/android/app/build.gradle.kts"], "android tests")
        self.assertEqual(check.cmd[-2:], ("testDebugUnitTest", ":core:model:test"))

    def test_a_changed_feature_test_runs_only_in_its_module(self):
        check = find(["frontend/android/feature/send/src/test/java/app/nzela/ui/amount/AmountTest.kt"], "android tests")
        self.assertEqual(check.cmd[-3:], (":feature:send:testDebugUnitTest", "--tests", "app.nzela.ui.amount.AmountTest"))

    def test_the_pure_model_module_is_tested_by_its_own_task(self):
        check = find(["frontend/android/core/model/src/test/java/app/nzela/core/MoneyFormatTest.kt"], "android tests")
        self.assertEqual(check.cmd[-3], ":core:model:test")

    def test_known_changes_keep_their_scoped_check_commands(self):
        everything = ["crates/nzela/src/lib.rs", "frontend/admin/src/App.tsx", "frontend/android/app/src/main/java/app/nzela/ui/SendScreen.kt",
                      "tools/nzfleet/nzfleet/plan.py"]
        for check in plan.proof_plan(everything, TRACKED + ["tools/nzfleet/x"], "BASE"):
            self.assertNotEqual(check.cmd, ("scripts/checks.sh", "all"))

    def test_a_changed_shell_script_is_syntax_checked(self):
        check = find(["scripts/checks.sh"], "shell syntax")
        self.assertEqual(check.cmd[-1], "scripts/checks.sh")

    def test_a_change_to_nzfleet_runs_its_tests(self):
        self.assertEqual(find(["tools/nzfleet/nzfleet/plan.py"], "nzfleet tests").cwd, "tools/nzfleet")

    def test_a_gitignore_change_keeps_the_mapped_proof_plan(self):
        self.assertEqual(names([".gitignore"]), ["whitespace", "secrets"])


class Errors(unittest.TestCase):
    def test_the_first_error_is_the_first_line_that_says_one(self):
        log = "Compiling a\nwarning: unused\nerror[E0308]: mismatched types\n --> src/a.rs:3\n\nerror: aborting"
        self.assertTrue(plan.first_error(log).startswith("error[E0308]: mismatched types"))

    def test_a_failed_test_is_named(self):
        self.assertIn("worker::tests::slow", plan.first_error("running 2 tests\ntest worker::tests::slow ... FAILED\n"))

    def test_a_typescript_error_is_found(self):
        self.assertIn("TS2322", plan.first_error("> tsc\nsrc/a.tsx(1,2): error TS2322: nope"))

    def test_a_log_with_no_recognisable_error_gives_its_last_lines(self):
        self.assertEqual(plan.first_error("a\nb\n\nc\n"), "a\nb\nc")


class Proof(unittest.TestCase):
    ok = {"ok": True, "tree": "T", "checks": []}

    def test_worker_capture_uses_the_current_visual_plans_renderer_names(self):
        observed = {"version": 1, "base": "base", "head": "WORKTREE:head:digest",
                    "full_fallback": {"console": False, "android": False},
                    "renderer": {"console": ["03-home"]}, "selected": {"android": ["home", "home_NIGHT"]}}
        self.assertEqual(prove.capture_filter(observed, "console", "base", "head"), "03-home")
        self.assertEqual(prove.capture_filter(observed, "android", "base", "head"), "home,home_NIGHT")

    def test_worker_capture_does_not_accept_a_stale_or_incomplete_visual_plan(self):
        valid = {"version": 1, "base": "base", "head": "WORKTREE:head:digest",
                 "full_fallback": {"android": False}, "selected": {"android": ["home"]}}
        for changed in ({"version": 2}, {"base": "old"}, {"head": "WORKTREE:old:digest"},
                        {"full_fallback": {"android": True}}, {"selected": {"android": []}},
                        {"selected": {"android": [None]}}):
            with self.subTest(changed=changed):
                self.assertEqual(prove.capture_filter({**valid, **changed}, "android", "base", "head"), "")

    def test_a_missing_visual_planner_keeps_the_complete_capture(self):
        self.assertEqual(prove.capture_selection("/no-such-project", "base", "android"), "")

    def test_malformed_visual_plan_objects_keep_the_complete_capture(self):
        valid = {"version": 1, "base": "base", "head": "WORKTREE:head:digest",
                 "full_fallback": {"android": False}, "selected": {"android": ["home"]}}
        for observed in (None, [], {**valid, "full_fallback": None}, {**valid, "selected": None}):
            with self.subTest(observed=observed):
                self.assertEqual(prove.capture_filter(observed, "android", "base", "head"), "")

    def test_shunt_keeps_shared_builds_in_order_without_recording_inherited_credentials(self):
        checks = [Check("rust", ("cargo", "+1.98", "test")),
                  Check("console", ("npm", "test"), "frontend/admin"),
                  Check("android", ("./gradlew", "test"), "frontend/android", slot=True)]
        result = prove.shunt_plan(checks, "/tree with spaces", {"DATABASE_URL": "private-value"}, {}, "base", "/proof")
        self.assertEqual([r["area"] for r in result], ["rust", "console", "android"])
        self.assertNotIn("private-value", str(result))
        self.assertIn("'/tree with spaces/frontend/admin'", result[1]["cmd"])
        self.assertIn("NZELA_BASE=base", result[0]["cmd"])

    def test_shunt_starts_preview_inside_the_console_lane_before_browser_checks(self):
        checks = [Check("build", ("npm", "run", "build"), "frontend/admin"),
                  Check("browser", ("npm", "run", "e2e"), "frontend/admin", needs="preview")]
        result = prove.shunt_plan(checks, "/tree", {}, {}, "base", "/proof")
        self.assertEqual([r["area"] for r in result], ["console", "console"])
        self.assertNotIn("_preview", result[0]["cmd"])
        self.assertLess(result[1]["cmd"].index("_preview"), result[1]["cmd"].index("npm run e2e"))

    def test_a_shunt_result_must_cover_every_requested_command(self):
        expected = [{"name": "first", "cmd": "one"}, {"name": "second", "cmd": "two"}]
        for actual in ([], expected[:1], list(reversed(expected)),
                       [expected[0], {"name": "second", "cmd": "different"}]):
            with self.subTest(actual=actual), self.assertRaisesRegex(ValueError, "complete requested"):
                prove.shunt_results({"checks": actual}, expected, [], "/tree", "/proof")

    def test_a_shunt_result_preserves_the_full_log_and_actual_duration(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(root + "/.tachoproof/logs")
            os.makedirs(root + "/.nzproof")
            with open(root + "/.tachoproof/logs/000.log", "w") as f:
                f.write("actual output\n")
            checks = [Check("unit", ("npm", "test"))]
            expected = [{"name": "unit", "cmd": "npm test"}]
            summary = {"checks": [{**expected[0], "ok": True, "duration_ms": 1234}]}
            result = prove.shunt_results(summary, expected, checks, root, root + "/.nzproof")
            self.assertEqual(result[0]["seconds"], 1)
            self.assertEqual(result[0]["cmd"], ["npm", "test"])
            with open(result[0]["log"]) as f:
                self.assertEqual(f.read(), "actual output\n")

    def test_no_proof_is_refused(self):
        self.assertIn("nzfleet prove", plan.proof_problems(None, "T", False)[0])

    def test_a_failed_proof_names_the_check_and_its_error(self):
        proof = {"ok": False, "checks": [{"name": "rust fmt", "ok": False, "error": "Diff in a.rs"}]}
        self.assertIn("failed at rust fmt: Diff in a.rs", plan.proof_problems(proof, "T", False)[0])

    def test_a_proof_of_other_files_is_older_than_the_commit(self):
        self.assertIn("older than the last commit", plan.proof_problems(self.ok, "OTHER", False)[0])

    def test_uncommitted_changes_are_refused(self):
        self.assertIn("uncommitted", plan.proof_problems(self.ok, "T", True)[0])

    def test_a_proof_of_exactly_this_tree_stands(self):
        self.assertEqual(plan.proof_problems(self.ok, "T", False), [])

    def test_a_proof_covers_a_commit_that_drops_redrawn_non_golden_shots(self):
        changes = ["frontend/admin/screenshots/05-transfer-detail-waiting.png"]
        problems = plan.proof_problems(self.ok, "OTHER", False, changes, set())
        self.assertEqual(problems, [])

    def test_a_proof_does_not_cover_a_commit_that_changes_source(self):
        changes = ["crates/nzela/src/lib.rs"]
        problems = plan.proof_problems(self.ok, "OTHER", False, changes, set())
        self.assertIn("older than the last commit", problems[0])

    def test_a_proof_does_not_cover_a_commit_that_drops_a_moved_golden_shot(self):
        golden = {"frontend/admin/screenshots/01-signin.png"}
        problems = plan.proof_problems(self.ok, "OTHER", False, list(golden), golden)
        self.assertIn("older than the last commit", problems[0])

    def test_rust_checks_receive_the_existing_compiler_cache(self):
        check = Check("rust tests", ("cargo", "+1.98", "test"))
        env = prove.check_environment(check, {"PATH": "/bin"}, {"RUSTC_WRAPPER": "/tree/scripts/rustc-wrapper.sh"}, "gradle-slot", "base", "/wt")
        self.assertEqual(env["RUSTC_WRAPPER"], "/tree/scripts/rustc-wrapper.sh")
        self.assertNotIn("NZELA_GRADLE_SLOT", env)

    def test_conservative_checks_receive_both_reusable_caches_and_the_android_slot(self):
        check = Check("conservative checks", ("scripts/checks.sh", "all"))
        env = prove.check_environment(check, {"PATH": "/bin"}, {"RUSTC_WRAPPER": "/tree/scripts/rustc-wrapper.sh"}, "gradle-slot", "base", "/wt")
        self.assertEqual(env["RUSTC_WRAPPER"], "/tree/scripts/rustc-wrapper.sh")
        self.assertEqual(env["NZELA_GRADLE_SLOT"], "gradle-slot")


    def test_a_proof_keeps_gradle_daemons_in_a_registry_inside_its_own_directory(self):
        check = Check("conservative checks", ("scripts/checks.sh", "all"))
        env = prove.check_environment(check, {"PATH": "/bin"}, {}, "", "abc", "/wt/.nzproof")
        self.assertEqual(env["GRADLE_OPTS"], "-Dorg.gradle.daemon.registry.base=/wt/.nzproof/gradle-daemons")

    def test_every_check_is_told_the_base_the_change_is_proved_against(self):
        check = Check("conservative checks", ("scripts/checks.sh", "all"))
        env = prove.check_environment(check, {"PATH": "/bin"}, {}, "", "abc123", "/wt")
        self.assertEqual(env["NZELA_BASE"], "abc123")

class Refusals(unittest.TestCase):
    ok = {"ok": True, "tree": "T", "checks": []}

    def refuse(self, **kw):
        args = dict(files=["a.rs"], commits=[("c" * 40, "A thing that is true\n\nBecause.")], title="A thing that is true",
                    description="Because.", merges=[], proof=self.ok, tree="T", dirty=False)
        args.update(kw)
        return plan.pr_refusals(args["files"], args["commits"], args["title"], args["description"], args["merges"],
                                args["proof"], args["tree"], args["dirty"])

    def test_a_clean_branch_is_not_refused(self):
        self.assertEqual(self.refuse(), [])

    def test_an_empty_diff_is_refused(self):
        self.assertIn("changes nothing", self.refuse(files=[])[0])

    def test_a_co_authored_by_trailer_in_a_commit_is_refused_by_sha(self):
        got = self.refuse(commits=[("abcdef1234", "Fix\n\nCo-Authored-By: Someone <a@b>")])
        self.assertTrue(got and "commit abcdef12" in got[0] and "Co-Authored-By" in got[0])

    def test_generated_with_in_the_description_is_refused(self):
        self.assertTrue(self.refuse(description="Done.\n\nGenerated with a tool"))

    def test_a_session_link_is_refused(self):
        self.assertTrue(self.refuse(description="see https://claude.ai/code/session_01abc"))

    def test_merge_commits_are_refused_with_how_to_fix(self):
        got = self.refuse(merges=["1234567890"])
        self.assertIn("12345678", got[0])
        self.assertIn("plain commits", got[0])

    def test_a_missing_proof_is_refused(self):
        self.assertIn("nzfleet prove", self.refuse(proof=None)[0])

    def test_an_empty_title_is_refused(self):
        self.assertIn("title", self.refuse(title="")[0])

    def test_every_reason_is_given_at_once(self):
        got = self.refuse(files=[], proof=None, merges=["1234567890"])
        self.assertEqual(len(got), 3)

    def test_ordinary_prose_about_sessions_and_generation_is_not_attribution(self):
        self.assertEqual(plan.attribution("Sessions expire; the code generator is rerun and the user is signed in."), [])

    def test_the_body_carries_the_proof_and_the_stack(self):
        proof = {"head": "a" * 40, "checks": [{"name": "rust fmt", "ok": True, "seconds": 3, "cmd": ["cargo", "fmt"], "output": "fine"}]}
        body = plan.build_body("Why.", proof, 816)
        self.assertIn("Stacks on #816.", body)
        self.assertIn("**rust fmt**: passed in 3s", body)
        self.assertIn("$ cargo fmt\nfine", body)


class Setup(unittest.TestCase):
    def test_a_slug_is_a_branch_name(self):
        self.assertEqual(plan.slug_problem("a-held-rate-can-be-discarded"), "")
        for bad in ("", "ab", "Has-Caps", "two--dashes", "trailing-", "has space", "x" * 81):
            self.assertTrue(plan.slug_problem(bad), bad)

    def test_the_first_free_block_is_given(self):
        self.assertEqual(plan.allocate_ports(set(), lambda p: False), 20000)
        self.assertEqual(plan.allocate_ports({20000}, lambda p: False), 20020)

    def test_a_block_with_one_listener_is_skipped(self):
        self.assertEqual(plan.allocate_ports(set(), lambda p: p == 20007), 20020)

    def test_two_agents_never_get_the_same_block(self):
        taken = set()
        for _ in range(5):
            taken.add(plan.allocate_ports(taken, lambda p: False))
        self.assertEqual(len(taken), 5)

    def test_blocks_stay_below_the_ephemeral_range(self):
        self.assertLess(plan.PORT_LOW + plan.PORT_SIZE * plan.PORT_BLOCKS, 32768)

    def test_no_block_is_left_when_all_are_taken(self):
        taken = {plan.PORT_LOW + i * plan.PORT_SIZE for i in range(plan.PORT_BLOCKS)}
        self.assertIsNone(plan.allocate_ports(taken, lambda p: False))

    def test_migrations_apply_in_file_name_order_across_crates(self):
        got = plan.migration_order(["crates/nzela/migrations/0044_b.sql", "crates/nzela-core/migrations/0043_a.sql",
                                    "crates/nzela/migrations/20260901000000_c.sql", "crates/nzela/migrations/0100_d.sql"])
        self.assertEqual([p.rsplit("/", 1)[1] for p in got], ["0043_a.sql", "0044_b.sql", "0100_d.sql", "20260901000000_c.sql"])

    def test_the_cache_key_follows_the_lock_file(self):
        self.assertEqual(plan.lock_key(b"a"), plan.lock_key(b"a"))
        self.assertNotEqual(plan.lock_key(b"a"), plan.lock_key(b"b"))

    def test_the_cache_is_ready_building_or_absent(self):
        self.assertEqual(plan.node_cache_state(True, True), "ready")
        self.assertEqual(plan.node_cache_state(False, True), "building")
        self.assertEqual(plan.node_cache_state(False, False), "absent")

    def test_the_env_file_survives_being_sourced_and_read_back(self):
        values = {"A": "x y", "B": "it's", "C": 3, "D": ""}
        self.assertEqual(plan.parse_env(plan.env_file(values)), {"A": "x y", "B": "it's", "C": "3", "D": ""})

    def test_path_gains_the_toolchain_once_and_in_front(self):
        self.assertEqual(plan.merge_path("/c/bin:/usr/bin", "/usr/bin:/bin"), "/c/bin:/usr/bin:/bin")

    def test_a_process_is_ours_only_inside_the_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            inner = os.path.join(root, "a")
            os.mkdir(inner)
            self.assertTrue(plan.owns(inner, root))
            self.assertFalse(plan.owns(tempfile.gettempdir(), root))
            self.assertFalse(plan.owns(root + "-other", root))


if __name__ == "__main__":
    unittest.main()
