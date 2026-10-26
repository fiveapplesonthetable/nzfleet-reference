"""Properties of the conservative capture planner."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("visual_plan", Path(__file__).with_name("visual-plan.py"))
visual_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual_plan)

_read_leaf_context = visual_plan.leaf_context
_head_leaf_context = []


def leaf_context_once(ref):
    """The tree at HEAD cannot change during a run, so it is read once; callers get their own copy."""
    if ref != "HEAD":
        return _read_leaf_context(ref)
    if not _head_leaf_context:
        _head_leaf_context.append(_read_leaf_context(ref))
    return dict(_head_leaf_context[0])


visual_plan.leaf_context = leaf_context_once


class VisualPlanTest(unittest.TestCase):
    def plan(self, changes, console_before=None, console_after=None, android_before=None, android_after=None):
        return visual_plan.plan(
            "base", "head", changes,
            console_before or set(), console_after or set(),
            android_before or set(), android_after or set(),
        )

    def test_a_new_console_registration_is_in_the_full_capture(self):
        result = self.plan([("M", "frontend/admin/scripts/screenshots.mjs", "frontend/admin/scripts/screenshots.mjs")],
                           {"01-signin"}, {"01-signin", "02-dashboard"})
        self.assertEqual(result["selected"]["console"], ["01-signin", "02-dashboard"])
        self.assertTrue(result["full_fallback"]["console"])

    def test_a_removed_registration_is_reported(self):
        result = self.plan([("M", "frontend/admin/scripts/screenshots.mjs", "frontend/admin/scripts/screenshots.mjs")],
                           {"01-signin", "02-dashboard"}, {"01-signin"})
        self.assertEqual(result["deleted"]["console"], ["02-dashboard"])

    def test_a_renamed_screen_selects_the_whole_affected_catalogue(self):
        result = self.plan([("R100", "frontend/admin/src/screens/Old.tsx", "frontend/admin/src/screens/New.tsx")],
                           {"01-signin"}, {"01-signin"})
        self.assertEqual(result["selected"]["console"], ["01-signin"])

    def test_a_shared_input_widens_both_frontends(self):
        result = self.plan([("M", "tools/nzfleet/shots/visual-plan.py", "tools/nzfleet/shots/visual-plan.py")],
                           {"01-signin"}, {"01-signin"}, {"state-home-loading"}, {"state-home-loading"})
        self.assertEqual(result["full_fallback"], {"console": True, "android": True})

    def test_an_unmapped_android_input_falls_back_to_all_images(self):
        result = self.plan([("A", None, "frontend/android/app/src/main/res/font/new.ttf")],
                           android_after={"state-home-loading", "state-add-number-submitting_NIGHT"})
        self.assertEqual(result["selected"]["android"], ["state-add-number-submitting_NIGHT", "state-home-loading"])

    def android_plan(self, changes, context):
        names = {"state-home-loading", "state-home-loading_NIGHT", "state-review-ready", "state-review-ready_NIGHT",
                 "SignInPreview.dark_W390dp_H844dp_NIGHT", "31-home-fr-dark"}
        return visual_plan.plan("base", "head", changes, set(), set(), names, names, android_context=context)

    CONTEXT = {"golden": {"SignInPreview.dark_W390dp_H844dp_NIGHT"},
               "light": {"state-home-loading", "state-review-ready"},
               "leaves": {"frontend/android/app/src/main/java/app/nzela/ui/home/HomeScreen.kt": {"state-home-loading"}}}

    def test_a_leaf_screen_draws_its_light_states_and_the_golden_set(self):
        path = "frontend/android/app/src/main/java/app/nzela/ui/home/HomeScreen.kt"
        result = self.android_plan([("M", path, path)], self.CONTEXT)
        self.assertEqual(result["selected"]["android"], ["SignInPreview.dark_W390dp_H844dp_NIGHT", "state-home-loading"])
        self.assertFalse(result["full_fallback"]["android"])

    def test_a_shared_android_input_draws_every_light_state_and_no_other_dark(self):
        path = "frontend/android/app/src/main/java/app/nzela/ui/components/Buttons.kt"
        result = self.android_plan([("M", path, path)], self.CONTEXT)
        self.assertEqual(result["selected"]["android"],
                         ["SignInPreview.dark_W390dp_H844dp_NIGHT", "state-home-loading", "state-review-ready"])

    def test_a_console_only_change_still_draws_the_golden_android_set(self):
        path = "frontend/admin/src/screens/Reports.tsx"
        result = self.android_plan([("M", path, path)], self.CONTEXT)
        self.assertEqual(result["selected"]["android"], ["SignInPreview.dark_W390dp_H844dp_NIGHT"])

    def test_a_dark_stem_is_recognised_by_either_naming(self):
        self.assertTrue(visual_plan.android_is_dark("state-home-loading_NIGHT"))
        self.assertTrue(visual_plan.android_is_dark("31-home-fr-dark"))
        self.assertFalse(visual_plan.android_is_dark("30-home-fr-light"))

    def test_the_real_android_leaves_cover_the_home_directory(self):
        names = visual_plan.android_catalogue(None)
        leaves = visual_plan.android_leaves(None, names)
        home = leaves["frontend/android/feature/home" + visual_plan.ANDROID_UI + "home/HomeScreen.kt"]
        self.assertIn("state-home-loading", home)
        self.assertNotIn("state-home-loading_NIGHT", home)
        self.assertNotIn("frontend/android/core/design" + visual_plan.ANDROID_UI + "components/Buttons.kt", leaves)

    def android_leaves_of(self, files):
        ui = visual_plan.ANDROID_UI
        sources = {f"frontend/android/app{ui}{path}": text for path, text in files.items()}
        states = {"States.kt": 'x(screen = "home") { HomeScreen() }\n'}

        def kotlin(ref, prefix, recursive):
            return sources if prefix == visual_plan.ANDROID_SOURCE else states

        with patch.object(visual_plan, "kotlin_sources", side_effect=kotlin):
            return visual_plan.android_leaves(None, {"state-home-loading"})

    def test_a_composable_used_in_another_directory_is_not_a_leaf(self):
        home = "home/HomeScreen.kt"
        own = "fun HomeScreen() {}\n"
        alone = self.android_leaves_of({home: own, "send/SendScreen.kt": "fun SendScreen() {}\n"})
        shared = self.android_leaves_of({home: own, "send/SendScreen.kt": "fun SendScreen() { HomeScreen() }\n"})
        self.assertEqual(list(alone.values()), [{"state-home-loading"}])
        self.assertEqual(shared, {})

    def test_a_longer_name_containing_a_composable_does_not_share_it(self):
        leaves = self.android_leaves_of({"home/HomeScreen.kt": "fun HomeScreen() {}\n",
                                         "send/SendScreen.kt": "fun SendScreen() { HomeScreenBanner() }\n"})
        self.assertEqual(list(leaves.values()), [{"state-home-loading"}])

    def test_the_real_console_catalogue_has_both_physical_themes(self):
        names = visual_plan.console_shots("HEAD")
        stems = visual_plan.console_catalogue("HEAD")
        self.assertGreater(len(names), 0)
        self.assertEqual(len(stems), 2 * len(names))
        self.assertEqual(visual_plan.console_names_from_stems({"11-reports-dark"}, visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)), {"11-reports"})

    def test_a_base_name_ending_in_dark_is_not_stripped(self):
        source = "const SHOTS = [\n  { name: '01-midnight-dark' },\n]\n"
        self.assertEqual(visual_plan.console_names_from_stems({"01-midnight-dark"}, source), {"01-midnight-dark"})
        with patch.object(visual_plan, "console_shots", return_value=["01-midnight", "01-midnight-dark"]):
            with self.assertRaisesRegex(ValueError, "collide"):
                visual_plan.console_catalogue("HEAD")

    def test_a_sign_in_leaf_with_unverified_other_actions_uses_full_capture(self):
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        result = visual_plan.plan("base", "head", [("M", "frontend/admin/src/screens/SignIn.tsx", "frontend/admin/src/screens/SignIn.tsx")],
                                  set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, visual_plan.leaf_context("HEAD"))
        self.assertEqual(len(result["selected"]["console"]), len(visual_plan.console_catalogue("HEAD")))
        self.assertTrue(result["full_fallback"]["console"])

    def test_a_sign_in_leaf_rejects_relative_alias_consumers(self):
        consumers, uncertain = visual_plan.sign_in_references("HEAD")
        self.assertEqual(consumers, {"frontend/admin/src/app/App.tsx"})
        self.assertFalse(uncertain)

    def test_quoted_from_text_is_not_a_module_declaration(self):
        source = """const origin = params.get('from')
const message = "export * from 'unlisted-package'"
// import { Screen } from '@/screens/SignIn'
import { Screen } from '@/screens/SignIn'
export { Screen as Entry } from './SignIn'
"""
        modules, uncertain = visual_plan.module_specifiers(source)
        self.assertEqual(modules, ["@/screens/SignIn", "./SignIn"])
        self.assertFalse(uncertain)

    def test_nested_template_loader_widens_sign_in_discovery(self):
        modules, uncertain = visual_plan.module_specifiers('const screen = `${({a: 1}).a && import("@/screens/SignIn")}`;')
        self.assertEqual(modules, [])
        self.assertTrue(uncertain)

    def test_aliased_require_widens_sign_in_discovery(self):
        modules, uncertain = visual_plan.module_specifiers('const load = require; load("@/screens/SignIn");')
        self.assertEqual(modules, [])
        self.assertTrue(uncertain)

    def test_import_meta_glob_eager_widens_sign_in_discovery(self):
        modules, uncertain = visual_plan.module_specifiers('const screens = import.meta.globEager("./screens/*.tsx");')
        self.assertEqual(modules, [])
        self.assertTrue(uncertain)

    def test_dependency_tree_files_are_never_read_for_console_importers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "frontend/admin"
            src = root / "src"
            src.mkdir(parents=True)
            (src / "App.tsx").write_text("import('@/screens/SignIn')")
            dependency = root / "node_modules" / "package" / "index.js"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("unreadable dependency")
            original = Path.read_text

            def guarded_read(path, *args, **kwargs):
                if path == dependency:
                    raise AssertionError("dependency tree was read")
                return original(path, *args, **kwargs)

            with patch.object(visual_plan, "CONSOLE_SOURCE", str(root) + "/"), patch.object(visual_plan, "CONSOLE_MODULES", str(src) + "/"), patch.object(Path, "read_text", guarded_read):
                self.assertEqual(set(visual_plan.console_source_files(None)), {str(src / "App.tsx")})

    def test_template_import_is_uncertain_in_committed_and_working_tree_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "frontend/admin/src"
            source_dir.mkdir(parents=True)
            importer = source_dir / "Importer.ts"
            importer.write_text("const screen = 'Sign' + 'In'; import(`@/screens/${screen}`)\n")
            for name in ("frontend/admin/tsconfig.app.json", "frontend/admin/vite.config.ts", "frontend/admin/package.json"):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(visual_plan.source_at("HEAD", name))
            subprocess.run(("git", "init", "-q"), cwd=root, check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.invalid"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Test"), check=True)
            subprocess.run(("git", "-C", str(root), "add", "."), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "Add importer"), check=True)
            with patch.object(visual_plan, "CONSOLE_MODULES", "frontend/admin/src/"):
                with patch.object(visual_plan, "git", side_effect=lambda *args, **kw: subprocess.check_output(("git", *args), cwd=root, input=kw.get("stdin"))):
                    def fixture_source(ref, path):
                        if ref:
                            return subprocess.check_output(("git", "show", f"{ref}:{path}"), cwd=root, stderr=subprocess.DEVNULL).decode() if (root / path).exists() else ""
                        return (root / path).read_text() if (root / path).exists() else ""

                    with patch.object(visual_plan, "optional_source_at", side_effect=fixture_source):
                        self.assertTrue(visual_plan.sign_in_references("HEAD")[1])
                        importer.write_text("import(/* comment */ `@/screens/${screen}`)\n")
                        with patch.object(visual_plan, "CONSOLE_MODULES", str(source_dir) + "/"):
                            self.assertTrue(visual_plan.sign_in_references(None)[1])

    def test_help_wildcard_import_allows_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Help.kt"
            source.write_text("import  app.nzela.ui.help.*\n")
            with patch.object(visual_plan, "git", side_effect=AssertionError("working tree scan expected")):
                with patch("pathlib.Path.rglob", return_value=iter([source])):
                    with patch.object(visual_plan, "ANDROID_SOURCE", str(root) + "/"):
                        self.assertTrue(visual_plan.reference_paths(visual_plan.tree_sources(None, str(root) + "/"), r"import[[:space:]]+app\.nzela\.ui\.help\.\*", regex=True))

    def test_a_help_leaf_rejects_wildcard_package_imports(self):
        self.assertFalse(visual_plan.leaf_context("HEAD")["help_wildcard_import"])

    def test_an_isolated_routed_screen_with_unknown_actions_uses_full_capture(self):
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        result = visual_plan.plan("base", "head", [("M", "frontend/admin/src/screens/Audit.tsx", "frontend/admin/src/screens/Audit.tsx")],
                                  set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, visual_plan.leaf_context("HEAD"))
        self.assertTrue(result["full_fallback"]["console"])

    def test_another_routes_button_or_keyboard_action_can_open_the_changed_screen(self):
        context = visual_plan.leaf_context("HEAD")
        catalogue = """const SHOTS = [
  { name: '02-dashboard', route: '/', act: async (page) => page.getByRole('button', { name: 'Open' }).click() },
  { name: '12-audit', route: '/audit' },
]\n"""
        path = "frontend/admin/src/screens/Audit.tsx"
        for action in ("page.getByRole('button', { name: 'Open' }).click()", "page.keyboard.press('Enter')", "page.evaluate(() => window.location.assign('/audit'))", "openScreen(page)", "page.locator(selector).click()"):
            with self.subTest(action=action):
                source = catalogue.replace("page.getByRole('button', { name: 'Open' }).click()", action)
                names = {"02-dashboard", "02-dashboard-dark", "12-audit", "12-audit-dark"}
                result = visual_plan.plan("base", "head", [("M", path, path)], set(), names, set(), set(), source, context)
                self.assertTrue(result["full_fallback"]["console"])

    def test_a_routed_screen_with_no_other_action_keeps_a_closed_capture(self):
        source = """const SHOTS = [
  { name: '02-dashboard', route: '/' },
  { name: '12-audit', route: '/audit' },
]\n"""
        path = "frontend/admin/src/screens/Audit.tsx"
        names = {"02-dashboard", "02-dashboard-dark", "12-audit", "12-audit-dark"}
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), names, set(), set(), source, visual_plan.leaf_context("HEAD"))
        self.assertFalse(result["full_fallback"]["console"])
        self.assertEqual(result["renderer"]["console"], ["12-audit"])

    def test_the_base_route_closure_must_also_be_proved(self):
        source = """const SHOTS = [
  { name: '02-dashboard', route: '/' },
  { name: '12-audit', route: '/audit' },
]\n"""
        path = "frontend/admin/src/screens/Audit.tsx"
        names = {"02-dashboard", "02-dashboard-dark", "12-audit", "12-audit-dark"}
        after = visual_plan.leaf_context("HEAD")
        before = visual_plan.leaf_context("HEAD")
        before["screen_importers"]["Audit"] = {"frontend/admin/src/screens/lazy.tsx", "frontend/admin/src/Other.tsx"}
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), names, set(), set(), source, after,
                                  source, before)
        self.assertTrue(result["full_fallback"]["console"])

    def test_unchanged_input_does_not_scan_screen_dependencies(self):
        with patch.object(visual_plan, "leaf_context", side_effect=AssertionError("unneeded import scan")):
            with patch.object(visual_plan, "changes_between", return_value=[]):
                with patch.object(sys, "argv", ["visual-plan.py", "--base", "HEAD", "--head", "HEAD", "--json"]):
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        visual_plan.main()
        self.assertEqual(json.loads(output.getvalue())["selected"]["console"], [])

    def test_the_base_help_closure_must_also_be_proved(self):
        path = visual_plan.ANDROID_HELP_SCREEN
        after = visual_plan.leaf_context("HEAD")
        before = visual_plan.leaf_context("HEAD")
        before["help_screen_users"] = before["help_screen_users"] | {"frontend/android/app/src/main/java/app/nzela/ui/Other.kt"}
        names = visual_plan.android_catalogue("HEAD")
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), set(), names, names,
                                  source, after, source, before)
        self.assertTrue(result["full_fallback"]["android"])

    def test_a_second_consumer_or_dynamic_import_restores_full_capture(self):
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        context = visual_plan.leaf_context("HEAD")
        path = "frontend/admin/src/screens/Audit.tsx"
        context["screen_importers"]["Audit"] = {"frontend/admin/src/screens/lazy.tsx", "frontend/admin/src/Other.tsx"}
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, context)
        self.assertTrue(result["full_fallback"]["console"])
        context["screen_importers"]["Audit"] = {"frontend/admin/src/screens/lazy.tsx"}
        context["screen_uncertain"] = True
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, context)
        self.assertTrue(result["full_fallback"]["console"])

    def test_a_changed_registry_or_route_uses_the_full_catalogue(self):
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        path = "frontend/admin/src/screens/Audit.tsx"
        for second in (visual_plan.CONSOLE_CATALOGUE, "frontend/admin/src/lib/sitemap.ts"):
            result = visual_plan.plan("base", "head", [("M", path, path), ("M", second, second)],
                                      set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, visual_plan.leaf_context("HEAD"))
            self.assertTrue(result["full_fallback"]["console"])

    def test_an_unresolved_navigation_action_widens_the_capture(self):
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        source = source.replace("{ name: '11-reports', route: '/audit/reports', wait:",
                                "{ name: '11-reports', act: async (page) => page.getByRole('link', { name: 'Audit' }).click(), route: '/audit/reports', wait:")
        path = "frontend/admin/src/screens/Audit.tsx"
        result = visual_plan.plan("base", "head", [("M", path, path)], set(), visual_plan.console_catalogue("HEAD"),
                                  set(), set(), source, visual_plan.leaf_context("HEAD"))
        self.assertTrue(result["full_fallback"]["console"])

    def test_a_help_leaf_selects_its_states_and_previews(self):
        names = visual_plan.android_catalogue("HEAD")
        result = visual_plan.plan("base", "head", [("M", visual_plan.ANDROID_HELP_SCREEN, visual_plan.ANDROID_HELP_SCREEN)],
                                  set(), set(), set(), names, "", visual_plan.leaf_context("HEAD"))
        self.assertLess(len(result["selected"]["android"]), len(names))
        self.assertIn("state-help-questions", result["selected"]["android"])
        self.assertIn("HelpDarkPreview.dark_W390dp_H844dp_NIGHT", result["selected"]["android"])

    def test_a_shared_component_and_an_unknown_importer_broaden(self):
        names = visual_plan.android_catalogue("HEAD")
        for path in ("frontend/android/app/src/main/java/app/nzela/ui/design/DesignTheme.kt", "frontend/android/app/src/main/java/app/nzela/ui/help/Unmapped.kt"):
            result = visual_plan.plan("base", "head", [("M", path, path)], set(), set(), set(), names, "")
            self.assertTrue(result["full_fallback"]["android"])

    def test_an_extra_importer_invalidates_the_leaf_closure(self):
        context = visual_plan.leaf_context("HEAD")
        context["sign_in_importers"] = context["sign_in_importers"] | {"frontend/admin/src/screens/Other.tsx"}
        source = visual_plan.source_at("HEAD", visual_plan.CONSOLE_CATALOGUE)
        result = visual_plan.plan("base", "head", [("M", "frontend/admin/src/screens/SignIn.tsx", "frontend/admin/src/screens/SignIn.tsx")],
                                  set(), visual_plan.console_catalogue("HEAD"), set(), set(), source, context)
        self.assertTrue(result["full_fallback"]["console"])

    def test_generated_local_runtime_files_are_not_capture_inputs(self):
        self.assertTrue(visual_plan.generated_untracked("scripts/__pycache__/visual-plan.cpython-312.pyc"))
        self.assertTrue(visual_plan.generated_untracked("frontend/android/app/hs_err_pid2704292.log"))
        self.assertFalse(visual_plan.generated_untracked("frontend/android/app/src/main/java/app/nzela/ui/help/NewScreen.kt"))


if __name__ == "__main__":
    unittest.main()
