import os
import sys
import unittest

from nzfleet.devloop import plan

K = "frontend/android/app/src/"
CORE_DEPENDENTS = {"nzela-core": {"nzela", "nzsim", "nzela-wire"}}


P = "crates/nzela-platform/"


def platform_sources(extra=None):
    """Platform and operator crates large enough to be selected by module, plus the given sources."""
    files = {f"{P}src/filler{i}.rs": "" for i in range(plan.SELECTABLE_FILES)}
    files.update({f"crates/nzela-operator-web/src/filler{i}.rs": "" for i in range(plan.SELECTABLE_FILES)})
    files.update(extra or {})
    return files


class RustSelection(unittest.TestCase):
    def test_a_core_edit_runs_core_whole_and_names_the_crates_left_to_ci(self):
        chosen = plan.rust_plan(["crates/nzela-core/src/money.rs"], CORE_DEPENDENTS, {})
        self.assertEqual(chosen["whole"], ["nzela-core"])
        self.assertEqual(chosen["downstream"], ["nzela", "nzela-wire", "nzsim"])

    def test_a_module_edit_in_a_large_crate_reaches_the_modules_that_name_it(self):
        sources = platform_sources({f"{P}src/store/a.rs": "pub fn a() {}", f"{P}src/api/b.rs": "use crate::store::a;", f"{P}src/other/c.rs": "fn c() {}"})
        chosen = plan.rust_plan([f"{P}src/store/a.rs"], {}, sources)
        self.assertEqual(chosen["modules"], ["nzela-platform:api", "nzela-platform:store"])

    def test_a_module_in_another_crate_that_names_the_changed_crate_is_reached(self):
        sources = platform_sources({f"{P}src/store/a.rs": "", "crates/nzela-operator-web/src/x/b.rs": "use nzela_platform::store::a;"})
        chosen = plan.rust_plan([f"{P}src/store/a.rs"], {}, sources, hops=1)
        self.assertEqual(chosen["modules"], ["nzela-operator-web:x", "nzela-platform:store"])

    def test_a_sibling_tests_file_runs_with_its_module(self):
        chosen = plan.rust_plan([f"{P}src/answer_tests.rs"], {}, platform_sources())
        self.assertEqual(chosen["modules"], ["nzela-platform:answer"])

    def test_an_integration_test_that_names_a_changed_module_is_selected(self):
        sources = platform_sources({f"{P}src/store/a.rs": "", "crates/nzela/tests/props.rs": "use nzela_platform::store::a;", "crates/nzela/tests/unrelated.rs": "fn x() {}"})
        chosen = plan.rust_plan([f"{P}src/store/a.rs"], {}, sources)
        self.assertEqual(chosen["tests"], ["nzela:props"])

    def test_a_crate_that_re_exports_a_module_reads_it_through_crate_paths(self):
        sources = platform_sources({f"{P}src/numbers/a.rs": "", "crates/nzela-operator-web/src/operator/reads.rs": "use crate::numbers::Error;"})
        chosen = plan.rust_plan([f"{P}src/numbers/a.rs"], {}, sources, hops=1)
        self.assertIn("nzela-operator-web:operator", chosen["modules"])

    def test_a_directory_holding_most_of_a_crate_is_split_one_level_further(self):
        deep = {f"{P}src/operator/m{i}/x.rs": "" for i in range(plan.SELECTABLE_FILES)}
        chosen = plan.rust_plan([f"{P}src/operator/m1/x.rs"], {}, deep)
        self.assertEqual(chosen["modules"], ["nzela-platform:operator::m1"])

    def test_a_small_crate_runs_whole_whatever_file_changed(self):
        chosen = plan.rust_plan(["crates/nzsim/src/a/b.rs"], {}, {"crates/nzsim/src/a/b.rs": ""})
        self.assertEqual(chosen["whole"], ["nzsim"])

    def test_a_migration_selects_everything(self):
        self.assertEqual(plan.rust_plan(["crates/nzela/migrations/1_x.sql"], {}, {})["mode"], "all")

    def test_a_lockfile_selects_everything(self):
        self.assertEqual(plan.rust_plan(["Cargo.lock"], {}, {})["mode"], "all")

    def test_a_file_outside_rust_leaves_rust_alone(self):
        self.assertEqual(plan.rust_plan(["docs/book/a.md"], {}, {})["mode"], "none")


class AndroidSelection(unittest.TestCase):
    files = {
        K + "main/java/p/Rate.kt": "package p\nclass Rate",
        K + "main/java/p/Quote.kt": "package p\nclass Quote(val rate: Rate)",
        K + "main/java/p/Unrelated.kt": "package p\nclass Unrelated",
        K + "test/java/p/QuoteTest.kt": "package p\nclass QuoteTest { @Test fun a() { Quote() } }",
        K + "test/java/p/UnrelatedTest.kt": "package p\nclass UnrelatedTest { @Test fun a() { Unrelated() } }",
    }

    def test_a_dependency_of_a_class_selects_the_tests_of_that_class(self):
        chosen = plan.android_plan([K + "main/java/p/Rate.kt"], self.files, hops=1)
        self.assertEqual(chosen["classes"], ["p.QuoteTest"])

    def test_a_changed_test_selects_itself(self):
        chosen = plan.android_plan([K + "test/java/p/UnrelatedTest.kt"], self.files)
        self.assertEqual(chosen["classes"], ["p.UnrelatedTest"])

    def test_a_resource_selects_everything(self):
        chosen = plan.android_plan(["frontend/android/app/src/main/res/values/s.xml"], self.files)
        self.assertEqual(chosen["mode"], "all")

    def test_a_deleted_main_file_selects_everything(self):
        chosen = plan.android_plan([K + "main/java/p/Gone.kt"], self.files)
        self.assertEqual(chosen["mode"], "all")


class ConsoleSelection(unittest.TestCase):
    modules = {
        "frontend/admin/tests/money.test.mjs": "import { fmt } from '../src/lib/money.ts'",
        "frontend/admin/tests/other.test.mjs": "import { x } from '../src/lib/other.ts'",
        "frontend/admin/tests/guard.test.mjs": "import fs from 'node:fs'",
        "frontend/admin/src/lib/money.ts": "import { n } from './number'",
        "frontend/admin/src/lib/number.ts": "export const n = 1",
        "frontend/admin/src/lib/other.ts": "export const x = 1",
    }
    tests = {p for p in modules if "/tests/" in p}

    def test_a_module_reached_through_an_import_selects_the_test_that_imports_it(self):
        chosen = plan.console_plan(["frontend/admin/src/lib/number.ts"], self.modules, self.tests)
        self.assertIn("frontend/admin/tests/money.test.mjs", chosen["tests"])
        self.assertNotIn("frontend/admin/tests/other.test.mjs", chosen["tests"])

    def test_a_test_that_reads_source_files_runs_for_any_source_change(self):
        chosen = plan.console_plan(["frontend/admin/src/lib/other.ts"], self.modules, self.tests)
        self.assertIn("frontend/admin/tests/guard.test.mjs", chosen["tests"])

    def test_a_package_change_selects_everything(self):
        self.assertEqual(plan.console_plan(["frontend/admin/package.json"], self.modules, self.tests)["mode"], "all")


if __name__ == "__main__":
    unittest.main()
