"""CLI regressions for dependency shapes that can widen a leaf capture."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]

# Every shot the committed catalogue names, in both themes: a full capture selects all of them.
_spec = importlib.util.spec_from_file_location("visual_plan", ROOT / "tools/nzfleet/shots/visual-plan.py")
_visual_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_visual_plan)
EVERY_SHOT = len(_visual_plan.console_catalogue("HEAD"))
FILES = (
    "frontend/admin/scripts/screenshots.mjs",
    "frontend/admin/src/app/App.tsx",
    "frontend/admin/src/app/routes.ts",
    "frontend/admin/src/screens/lazy.tsx",
    "frontend/admin/src/screens/SignIn.tsx",
    "frontend/admin/src/screens/Audit.tsx",
    "frontend/admin/src/lib/sitemap.ts",
    "frontend/admin/package.json",
    "frontend/admin/tsconfig.app.json",
    "frontend/admin/vite.config.ts",
    "frontend/android/docs/screenshots/catalogue.txt",
)


class VisualPlanCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "capture-test@example.invalid")
        self.git("config", "user.name", "Capture test")
        for name in FILES:
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(subprocess.check_output(("git", "show", f"HEAD:{name}"), cwd=ROOT))
        self.git("add", ".")
        self.git("commit", "-qm", "A sign-in capture fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.head = self.repo / "frontend/admin/src/screens/SignIn.tsx"
        self.change_sign_in()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(("git", *args), cwd=self.repo, text=True).strip()

    def run_plan(self, *args):
        output = subprocess.check_output(("python3", str(ROOT / "tools/nzfleet/shots/visual-plan.py"), "--base", self.base,
                                         "--head", *args, "--json"), cwd=self.repo, text=True)
        return json.loads(output)

    def add_importer(self, path, source):
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)

    def commit_graph_in_base(self, importers):
        for path, source in importers:
            self.add_importer(path, source)
        self.git("add", ".")
        self.git("commit", "-qm", "A committed sign-in dependency graph")
        self.base = self.git("rev-parse", "HEAD")
        self.change_sign_in()
        self.assertEqual(self.git("diff", "--name-only", self.base, "HEAD"), "frontend/admin/src/screens/SignIn.tsx")

    def change_sign_in(self):
        self.head.write_text(self.head.read_text() + "\n// capture input change\n")
        self.git("add", "frontend/admin/src/screens/SignIn.tsx")
        self.git("commit", "-qm", "A changed sign-in screen")

    def change_audit(self):
        self.base = self.git("rev-parse", "HEAD")
        audit = self.repo / "frontend/admin/src/screens/Audit.tsx"
        audit.write_text(audit.read_text() + "\n// fixture change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "A changed audit screen")

    def assert_full_sign_in_fallback(self, result):
        self.assertTrue(result["full_fallback"]["console"])
        self.assertEqual(len(result["selected"]["console"]), EVERY_SHOT)

    def test_a_sign_in_leaf_with_unverified_other_actions_is_full_in_the_real_cli(self):
        result = self.run_plan("HEAD")
        self.assertTrue(result["full_fallback"]["console"])
        self.assertEqual(len(result["selected"]["console"]), EVERY_SHOT)

    def test_an_audit_production_edit_with_unverified_actions_captures_every_shot(self):
        self.change_audit()
        result = self.run_plan("HEAD")
        self.assertTrue(result["full_fallback"]["console"])
        self.assertEqual(len(result["selected"]["console"]), EVERY_SHOT)

    def test_other_routes_button_and_keyboard_actions_widen_the_real_cli_plan(self):
        catalogue = self.repo / "frontend/admin/scripts/screenshots.mjs"
        for action in ("page.getByRole('button', { name: 'Open' }).click()", "page.keyboard.press('Enter')"):
            with self.subTest(action=action):
                catalogue.write_text("const SHOTS = [\n"
                                     "  { name: '02-dashboard', route: '/', act: async (page) => " + action + " },\n"
                                     "  { name: '12-audit', route: '/audit' },\n]\n")
                self.git("add", ".")
                self.git("commit", "-qm", "A registered dashboard action")
                self.change_audit()
                result = self.run_plan("HEAD")
                self.assertTrue(result["full_fallback"]["console"])
                self.assertEqual(len(result["selected"]["console"]), 4)
                self.base = self.git("rev-parse", "HEAD")

    def test_a_second_audit_importer_restores_full_capture_in_the_real_cli(self):
        self.add_importer("frontend/admin/src/Other.tsx", "export { Audit } from './screens/Audit'\n")
        self.git("add", ".")
        self.git("commit", "-qm", "A second audit consumer")
        self.change_audit()
        result = self.run_plan("HEAD")
        self.assertTrue(result["full_fallback"]["console"])
        self.assertEqual(len(result["selected"]["console"]), EVERY_SHOT)

    def test_a_nonliteral_audit_import_restores_full_capture_in_the_real_cli(self):
        self.add_importer("frontend/admin/src/Other.tsx", "export const load = (name: string) => import(`@/screens/${name}`)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "A dynamic screen consumer")
        self.change_audit()
        result = self.run_plan("HEAD")
        self.assertTrue(result["full_fallback"]["console"])
        self.assertEqual(len(result["selected"]["console"]), EVERY_SHOT)

    def test_relative_alias_importer_widens_the_real_cli_plan(self):
        self.commit_graph_in_base((("frontend/admin/src/auth/Other.tsx", "import { SignIn as Entry } from '../screens/SignIn'\n"),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))

    def test_nonliteral_dynamic_import_widens_the_real_cli_plan(self):
        self.commit_graph_in_base((("frontend/admin/src/Loader.tsx", "export const load = (moduleName: string) => import(moduleName)\n"),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))

    def test_barrel_reexport_widens_the_real_cli_plan(self):
        self.commit_graph_in_base((("frontend/admin/src/screens/index.ts", "export { SignIn as Entry } from './SignIn'\n"),
                                   ("frontend/admin/src/Other.tsx", "import { Entry } from './screens'\n")))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))

    def test_concatenated_literal_import_widens_committed_and_working_tree_plans(self):
        source = "import('@/screens/' + screen)\n"
        self.commit_graph_in_base((("frontend/admin/src/Loader.tsx", source),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))
        self.head.write_text(self.head.read_text() + "\n// changed screen\n")
        output = subprocess.check_output(("python3", str(ROOT / "tools/nzfleet/shots/visual-plan.py"), "--base", self.base,
                                          "--head", "HEAD", "--working-tree", "--json"), cwd=self.repo, text=True)
        self.assert_full_sign_in_fallback(json.loads(output))

    def test_comment_before_literal_import_widens_committed_and_working_tree_plans(self):
        source = "import(/* comment */ '@/screens/SignIn')\n"
        self.commit_graph_in_base((("frontend/admin/src/Loader.tsx", source),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))
        self.head.write_text(self.head.read_text() + "\n// changed screen\n")
        output = subprocess.check_output(("python3", str(ROOT / "tools/nzfleet/shots/visual-plan.py"), "--base", self.base,
                                          "--head", "HEAD", "--working-tree", "--json"), cwd=self.repo, text=True)
        self.assert_full_sign_in_fallback(json.loads(output))

    def test_comment_before_dynamic_import_parenthesis_widens_the_real_cli_plan(self):
        self.commit_graph_in_base((("frontend/admin/src/Loader.tsx", "import /*comment*/ ('@/screens/SignIn')\n"),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))

    def test_escaped_static_module_literal_widens_the_real_cli_plan(self):
        self.commit_graph_in_base((("frontend/admin/src/Loader.tsx", "import { SignInScreen } from '@/screens/\\u0053ignIn'\n"),))
        self.assert_full_sign_in_fallback(self.run_plan("HEAD"))

    def test_untracked_importer_widens_the_working_tree_plan(self):
        self.add_importer("frontend/admin/src/Other.tsx", "export { SignIn as Entry } from '../screens/SignIn'\n")
        output = subprocess.check_output(("python3", str(ROOT / "tools/nzfleet/shots/visual-plan.py"), "--base", self.base,
                                          "--head", "HEAD", "--working-tree", "--json"), cwd=self.repo, text=True)
        self.assert_full_sign_in_fallback(json.loads(output))


if __name__ == "__main__":
    unittest.main()
