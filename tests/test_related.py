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

    def test_a_script_outside_the_package_selects_everything(self):
        self.assertEqual(related.fleet(["tools/nzfleet/bin/nzfleet"], FLEET_TESTS), ["*"])

    def test_a_path_outside_the_fleet_selects_nothing(self):
        self.assertEqual(related.fleet(["crates/nzela/src/lib.rs"], FLEET_TESTS), [])


class AConsoleChangeRunsTheUnitTestsThatReachIt(unittest.TestCase):
    def test_a_library_module_selects_the_test_that_imports_it(self):
        self.assertEqual(related.console(["frontend/admin/src/lib/money.ts"], CONSOLE_MODULES, CONSOLE_TESTS),
                         ["tests/money.test.mjs"])

    def test_a_changed_config_selects_everything(self):
        self.assertEqual(related.console(["frontend/admin/package.json"], CONSOLE_MODULES, CONSOLE_TESTS), ["*"])


if __name__ == "__main__":
    unittest.main()
