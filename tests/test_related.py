"""Which unit tests the pre-push hook runs for a change under tools/nzfleet or the console."""

import unittest
import json
import subprocess
from pathlib import Path

from nzfleet.selection import related

FLEET_TESTS = {
    "tools/nzfleet/tests/test_plan.py": "from nzfleet import plan\n",
    "tools/nzfleet/tests/test_runs.py": "from nzfleet import runs, plan\n",
    "tools/nzfleet/tests/test_mail.py": "import unittest\nfrom nzfleet import mail\n# the plan is not imported here\n",
}
CONSOLE_MODULES = {
    "frontend/admin/src/lib/money.ts": "export const x = 1\n",
    "frontend/admin/tests/money.test.mjs": "import { x } from '../src/lib/money.ts'\n",
    "frontend/admin/tests/names.test.mjs": "import { y } from '../src/lib/names.ts'\n",
    "frontend/admin/src/lib/names.ts": "export const y = 1\n",
}
CONSOLE_TESTS = {"frontend/admin/tests/money.test.mjs", "frontend/admin/tests/names.test.mjs"}


class AFleetChangeRunsTheTestsThatImportTheModule(unittest.TestCase):
    def test_a_module_selects_the_tests_that_import_it_and_no_other(self):
        self.assertEqual(related.fleet(["tools/nzfleet/nzfleet/plan.py"], FLEET_TESTS), ["tests.test_plan", "tests.test_runs"])

    def test_a_changed_test_selects_itself(self):
        self.assertEqual(related.fleet(["tools/nzfleet/tests/test_mail.py"], FLEET_TESTS), ["tests.test_mail"])

    def test_a_module_selects_the_tests_of_the_modules_that_import_it(self):
        modules = {"tools/nzfleet/nzfleet/runs.py": "from . import plan\n", "tools/nzfleet/nzfleet/mail.py": "import os\n"}
        tests = {"tools/nzfleet/tests/test_runs.py": "from nzfleet import runs\n", "tools/nzfleet/tests/test_mail.py": "from nzfleet import mail\n"}
        self.assertEqual(related.fleet(["tools/nzfleet/nzfleet/plan.py"], tests, modules), ["tests.test_runs"])

    def test_a_script_selects_the_tests_that_name_it_and_never_everything(self):
        tests = {"tools/nzfleet/tests/test_cli.py": "run('bin/nzfleet', 'next')\n", "tools/nzfleet/tests/test_plan.py": "from nzfleet import plan\n"}
        self.assertEqual(related.fleet(["tools/nzfleet/bin/nzfleet"], tests), ["tests.test_cli"])

    def test_a_path_outside_the_fleet_selects_nothing(self):
        self.assertEqual(related.fleet(["crates/nzela/src/lib.rs"], FLEET_TESTS), [])


DEPLOY_CHECKS = [
    {"title": "images", "files": ["tools/nzfleet/release/image-test.sh"], "prefixes": [], "text": "tools/nzfleet/release/image.sh", "always": False},
    {"title": "terraform", "files": [], "prefixes": ["deploy/terraform/"], "text": "", "always": False},
    {"title": "no sleeps", "files": [], "prefixes": [], "text": "", "always": True},
]


class ADeployChangeRunsTheChecksThatReadIt(unittest.TestCase):
    def titles(self, path):
        return related.deploy([path], DEPLOY_CHECKS)

    def test_a_file_a_check_names_selects_that_check_and_the_guards(self):
        self.assertEqual(self.titles("tools/nzfleet/release/image.sh"), ["images", "no sleeps"])

    def test_a_file_under_a_directory_a_check_reads_selects_it(self):
        self.assertEqual(self.titles("deploy/terraform/main.tf"), ["terraform", "no sleeps"])

    def test_a_file_nothing_reads_selects_only_the_guards(self):
        self.assertEqual(self.titles("docs/book/README.md"), ["no sleeps"])


class AConsoleChangeRunsTheUnitTestsThatReachIt(unittest.TestCase):
    def test_a_library_module_selects_the_test_that_imports_it(self):
        self.assertEqual(related.console(["frontend/admin/src/lib/money.ts"], CONSOLE_MODULES, CONSOLE_TESTS),
                         ["tests/money.test.mjs"])

    def test_a_changed_config_selects_everything(self):
        self.assertEqual(related.console(["frontend/admin/package.json"], CONSOLE_MODULES, CONSOLE_TESTS), ["*"])


class AConsoleChangeRunsTheBrowserSpecsThatCanNavigateToIt(unittest.TestCase):
    modules = {
        "frontend/admin/src/pages/home/page.tsx": "export function Home() {}",
        "frontend/admin/e2e/home.spec.ts": "await page.goto('/')",
        "frontend/admin/e2e/other.spec.ts": "await page.goto('/other')",
    }

    def test_a_page_change_selects_specs_that_navigate_without_importing_it(self):
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/home/page.tsx"], self.modules),
                         ["e2e/home.spec.ts", "e2e/other.spec.ts"])

    def test_a_changed_spec_selects_only_itself(self):
        coverage = {"home.spec.ts": [], "other.spec.ts": []}
        self.assertEqual(related.console_specs(["frontend/admin/e2e/home.spec.ts"], self.modules, coverage),
                         ["e2e/home.spec.ts"])

    def test_a_build_configuration_change_selects_every_spec(self):
        self.assertEqual(related.console_specs(["frontend/admin/vite.config.ts"], self.modules),
                         ["e2e/home.spec.ts", "e2e/other.spec.ts"])
class AConsoleSourceChangeRunsTheBrowserSpecsThatExecuteIt(unittest.TestCase):
    modules = {
        "frontend/admin/e2e/home.spec.ts": "import { test } from './bundle'\n",
        "frontend/admin/e2e/senders.spec.ts": "import { test } from './bundle'\n",
        "frontend/admin/src/pages/Home.tsx": "export const home = 1\n",
        "frontend/admin/src/pages/Senders.tsx": "export const senders = 1\n",
    }
    coverage = {"home.spec.ts": ["src/pages/Home.tsx"], "senders.spec.ts": ["src/pages/Senders.tsx"]}

    def test_a_transitive_fixture_change_invalidates_the_recording(self):
        modules = {**self.modules,
                   "frontend/admin/e2e/home.spec.ts": "import './helper'",
                   "frontend/admin/e2e/helper.ts": "import '../fixtures/home.json'",
                   "frontend/admin/fixtures/home.json": '{"route":"/home"}'}
        before = related.coverage_key(modules)
        modules["frontend/admin/fixtures/home.json"] = '{"route":"/senders"}'
        after = related.coverage_key(modules)
        self.assertNotEqual(before["specs"]["home.spec.ts"], after["specs"]["home.spec.ts"])
        self.assertEqual(before["specs"]["senders.spec.ts"], after["specs"]["senders.spec.ts"])

    def test_a_field_named_from_does_not_become_an_import(self):
        modules = {**self.modules, "frontend/admin/src/pages/Home.tsx": "params.get('from'); params.get('to')"}
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/Home.tsx"], modules, self.coverage),
                         ["e2e/home.spec.ts"])

    def test_an_unresolved_import_selects_every_spec(self):
        for named in ("missing-package", "./Missing", "unknown-alias/Screen", "Unknown"):
            modules = {**self.modules, "frontend/admin/src/pages/Unknown.ts": "export const unknown = 1",
                       "frontend/admin/src/pages/Home.tsx": f"import'{named}'"}
            self.assertEqual(related.console_specs(["frontend/admin/src/pages/Home.tsx"], modules, self.coverage),
                             ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_a_resolved_external_dependency_is_a_leaf(self):
        modules = {**self.modules, "frontend/admin/src/pages/Home.tsx": "import { useState } from 'react'"}
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/Home.tsx"], modules, self.coverage, frozenset({"react"})),
                         ["e2e/home.spec.ts"])

    def test_a_mapped_page_selects_only_its_spec(self):
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/Home.tsx"], self.modules, self.coverage),
                         ["e2e/home.spec.ts"])

    def test_an_unmapped_source_selects_every_spec(self):
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/New.tsx"], self.modules, self.coverage),
                         ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_a_changed_config_selects_every_spec(self):
        self.assertEqual(related.console_specs(["frontend/admin/package.json"], self.modules, self.coverage),
                         ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_shared_layout_and_styles_select_every_spec_even_when_mapped(self):
        coverage = {name: [*sources, "src/app/SignedIn.tsx", "src/index.css"]
                    for name, sources in self.coverage.items()}
        for path in ("frontend/admin/src/app/SignedIn.tsx", "frontend/admin/src/index.css"):
            self.assertEqual(related.console_specs([path], self.modules, coverage),
                             ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_a_new_import_keeps_an_old_map_conservative(self):
        modules = {**self.modules, "frontend/admin/src/pages/Home.tsx":
                   "import { senders } from '@/pages/Senders'\n"}
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/Senders.tsx"], modules, self.coverage),
                         ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_a_lazy_route_does_not_make_an_untouched_page_reach_its_screen(self):
        modules = {**self.modules, "frontend/admin/src/app/routes.ts":
                   "const Senders = lazy(() => import('@/pages/Senders'))\n"}
        coverage = {name: [*sources, "src/app/routes.ts"] for name, sources in self.coverage.items()}
        self.assertEqual(related.console_specs(["frontend/admin/src/pages/Senders.tsx"], modules, coverage),
                         ["e2e/senders.spec.ts"])

    def test_a_non_typescript_e2e_change_selects_every_spec(self):
        for path in ("frontend/admin/e2e/fixtures/data.json", "frontend/admin/e2e/images/icon.svg"):
            self.assertEqual(related.console_specs([path], self.modules, self.coverage),
                             ["e2e/home.spec.ts", "e2e/senders.spec.ts"])

    def test_the_generator_and_selector_use_the_same_source_extensions(self):
        root = Path(__file__).resolve().parents[3]
        extensions = json.loads((root / "frontend/admin/e2e/source-extensions.json").read_text())
        result = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import { sourceExtensions } from './frontend/admin/scripts/source-extensions.mjs'; "
             "console.log(JSON.stringify(sourceExtensions))"],
            cwd=root, check=True, capture_output=True, text=True,
        )
        self.assertEqual(related.source_extensions(), tuple(extensions))
        self.assertEqual(json.loads(result.stdout), extensions)
        for extension in (".cjs", ".jsx", ".js"):
            self.assertIn(extension, extensions)

    def test_a_javascript_import_reaches_its_mapped_spec(self):
        for extension in (".cjs", ".jsx", ".js"):
            source = f"frontend/admin/src/pages/Shared{extension}"
            modules = {**self.modules, source: "export const shared = 1\n",
                       "frontend/admin/src/pages/Home.tsx": "import './Shared'\n"}
            coverage = {**self.coverage, "senders.spec.ts": [f"src/pages/Shared{extension}"]}
            self.assertEqual(related.console_specs([source], modules, coverage),
                             ["e2e/home.spec.ts", "e2e/senders.spec.ts"])


if __name__ == "__main__":
    unittest.main()
