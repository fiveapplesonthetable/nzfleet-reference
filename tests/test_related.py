"""Which unit tests the pre-push hook runs for a change under tools/nzfleet or the console."""

import unittest

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
        self.assertEqual(related.console_specs(["frontend/admin/e2e/home.spec.ts"], self.modules),
                         ["e2e/home.spec.ts"])

    def test_a_build_configuration_change_selects_every_spec(self):
        self.assertEqual(related.console_specs(["frontend/admin/vite.config.ts"], self.modules),
                         ["e2e/home.spec.ts", "e2e/other.spec.ts"])


if __name__ == "__main__":
    unittest.main()
