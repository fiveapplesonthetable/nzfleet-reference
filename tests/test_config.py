import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nzfleet import config
from nzfleet import dispatch


class EnvFile(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "env")
            with open(path, "w") as f:
                f.write(text)
            config._load(path)

    def tearDown(self):
        for key in ("NZFLEET_T_A", "NZFLEET_T_B", "#NZFLEET_T_C"):
            os.environ.pop(key, None)

    def test_the_file_fills_what_the_environment_lacks(self):
        self.load("NZFLEET_T_A=from file\n")
        self.assertEqual(os.environ["NZFLEET_T_A"], "from file")

    def test_the_environment_wins_over_the_file(self):
        os.environ["NZFLEET_T_B"] = "set"
        self.load("NZFLEET_T_B=from file\n")
        self.assertEqual(os.environ["NZFLEET_T_B"], "set")

    def test_a_commented_line_is_ignored(self):
        self.load("#NZFLEET_T_C=x\n")
        self.assertNotIn("#NZFLEET_T_C", os.environ)

    def test_a_semicolon_comment_is_ignored(self):
        self.assertEqual(config._entries("; ignored\nNZFLEET_T_A=kept\n"), [("NZFLEET_T_A", "kept")])

    def test_a_missing_file_is_no_error(self):
        config._load("/nonexistent/nzfleet/env")

    def test_a_quoted_command_is_unquoted_before_argv_parsing(self):
        self.load('NZFLEET_T_A="/opt/codex exec --model {model} {brief}"\n')
        self.assertEqual(os.environ["NZFLEET_T_A"], "/opt/codex exec --model {model} {brief}")

    def test_a_quoted_value_keeps_shell_characters_and_embedded_newlines_literal(self):
        value = "first $HOME `touch /tmp/should-not-exist`\nsecond"
        self.load("NZFLEET_T_A='" + value + "'\n")
        self.assertEqual(os.environ["NZFLEET_T_A"], value)

    def test_command_arguments_keep_the_brief_as_one_literal_argument(self):
        self.load('NZFLEET_T_A="/opt/codex exec --model {model} {brief}"\n')
        brief = "line one\n$HOME `literal`"
        argv = dispatch.command_argv(os.environ["NZFLEET_T_A"], "gpt-6-luna", brief)
        self.assertEqual(argv, ["/opt/codex", "exec", "--model", "gpt-6-luna", brief])

    def test_escaped_quotes_keep_a_command_argument_with_spaces_together(self):
        self.load('NZFLEET_T_A="/opt/codex exec --label \\"two words\\" {brief}"\n')
        argv = dispatch.command_argv(os.environ["NZFLEET_T_A"], "gpt-6-luna", "task")
        self.assertEqual(argv, ["/opt/codex", "exec", "--label", "two words", "task"])

    def test_unquoted_escapes_preserve_characters_and_join_continued_lines(self):
        self.assertEqual(
            config._entries("NZFLEET_T_A=one\\ two\\\n  three\\#four\\\\five\n"),
            [("NZFLEET_T_A", "one two  three#four\\five")],
        )

    def test_double_quotes_follow_systemd_escape_and_continuation_rules(self):
        self.assertEqual(
            config._entries('NZFLEET_T_A="one\\$two\\`three\\q\\\n  four"\n'),
            [("NZFLEET_T_A", "one$two`three\\q  four")],
        )

    def test_single_quotes_keep_multiline_values_verbatim(self):
        self.assertEqual(
            config._entries("NZFLEET_T_A='one\\n\ntwo'\n"),
            [("NZFLEET_T_A", "one\\n\ntwo")],
        )


class PrivateTemporaryDirectory(unittest.TestCase):
    def test_a_child_gets_a_private_directory_beside_the_scratch_worktrees(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "WORK", os.path.join(home, "work")):
            env = config.with_private_tmpdir({"TMPDIR": "/tmp", "MARKER": "kept"})
            self.assertEqual(env["TMPDIR"], os.path.join(home, "tmp"))
            self.assertEqual(env["MARKER"], "kept")
            self.assertEqual(os.stat(env["TMPDIR"]).st_mode & 0o777, 0o700)

    def test_a_directory_others_can_read_but_not_write_is_accepted(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "WORK", os.path.join(home, "work")):
            os.mkdir(os.path.join(home, "tmp"))
            os.chmod(os.path.join(home, "tmp"), 0o775)
            self.assertEqual(config.private_tmpdir(), os.path.join(home, "tmp"))

    def test_a_shared_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "WORK", os.path.join(home, "work")):
            path = os.path.join(home, "tmp")
            os.mkdir(path, mode=0o777)
            os.chmod(path, 0o777)
            with self.assertRaises(OSError):
                config.with_private_tmpdir()

    def test_a_relative_home_still_gives_children_an_absolute_private_directory(self):
        with tempfile.TemporaryDirectory() as root:
            before = os.getcwd()
            try:
                os.chdir(root)
                with mock.patch.object(config, "WORK", "fleet/work"):
                    env = config.with_private_tmpdir({"MARKER": "kept"})
                self.assertEqual(env["TMPDIR"], os.path.join(root, "fleet", "tmp"))
                os.mkdir(os.path.join(root, "elsewhere"))
                result = subprocess.run(
                    [sys.executable, "-c", "import tempfile; print(tempfile.mkstemp()[1])"],
                    cwd=os.path.join(root, "elsewhere"), env={**os.environ, **env},
                    capture_output=True, text=True, check=True,
                )
                path = result.stdout.strip()
                self.assertEqual(os.path.dirname(path), env["TMPDIR"])
                os.unlink(path)
            finally:
                os.chdir(before)

    def test_a_symlink_or_wrong_owner_is_refused(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(config, "WORK", os.path.join(home, "work")):
            outside = os.path.join(home, "outside")
            os.mkdir(outside, mode=0o700)
            path = os.path.join(home, "tmp")
            os.symlink(outside, path)
            with self.assertRaises(OSError):
                config.private_tmpdir()
            os.unlink(path)
            os.mkdir(path, mode=0o700)
            original = config.os.stat

            def wrong_owner(candidate, *args, **kwargs):
                info = original(candidate, *args, **kwargs)
                if candidate == path:
                    values = list(info)
                    values[4] = os.getuid() + 1
                    return os.stat_result(values)
                return info

            with mock.patch.object(config.os, "stat", side_effect=wrong_owner):
                with self.assertRaises(OSError):
                    config.private_tmpdir()


if __name__ == "__main__":
    unittest.main()
