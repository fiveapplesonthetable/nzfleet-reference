import os
import subprocess
import tempfile
import unittest
from unittest import mock

from nzfleet import premerge

CHECKS = """#!/bin/bash
set -euo pipefail
test "${NZELA_BASE:-}" = "$(git rev-parse HEAD^1)"
case "$TEST_MOVE" in
    main)
        git -C "$TEST_SOURCE" commit -q --allow-empty -m 'main moved'
        git -C "$TEST_SOURCE" push -q upstream main ;;
    head)
        git -C "$TEST_SOURCE" switch -q topic
        git -C "$TEST_SOURCE" commit -q --allow-empty -m 'head moved'
        git -C "$TEST_SOURCE" push -q upstream HEAD:refs/pull/1/head ;;
esac
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Premerge(unittest.TestCase):
    def fixture(self, move):
        base = tempfile.mkdtemp()
        source = os.path.join(base, "source")
        os.makedirs(os.path.join(source, "scripts"))
        os.makedirs(os.path.join(source, "frontend/admin"))
        os.makedirs(os.path.join(base, "bin"))
        git("init", "--bare", "-q", os.path.join(base, "upstream.git"), cwd=base)
        git("init", "-q", "-b", "main", source, cwd=base)
        for name, text in (("scripts/checks.sh", CHECKS), ("../bin/npm", "#!/bin/sh\n")):
            path = os.path.join(source, name)
            with open(path, "w") as out:
                out.write(text)
            os.chmod(path, 0o755)
        open(os.path.join(source, "frontend/admin/.keep"), "w").close()
        git("config", "user.name", "t", cwd=source)
        git("config", "user.email", "t@test.invalid", cwd=source)
        git("add", "scripts", "frontend", cwd=source)
        git("commit", "-qm", "main", cwd=source)
        git("remote", "add", "upstream", os.path.join(base, "upstream.git"), cwd=source)
        git("push", "-q", "upstream", "main", cwd=source)
        git("switch", "-qc", "topic", cwd=source)
        git("commit", "-q", "--allow-empty", "-m", "pull request", cwd=source)
        git("push", "-q", "upstream", "HEAD:refs/pull/1/head", cwd=source)
        git("switch", "-q", "main", cwd=source)
        env = {"PATH": f"{base}/bin:{os.environ['PATH']}", "TMPDIR": base,
               "TEST_SOURCE": source, "TEST_MOVE": move}
        return source, env

    def run_premerge(self, move):
        source, env = self.fixture(move)
        cwd = os.getcwd()
        os.chdir(source)
        try:
            with mock.patch.dict(os.environ, env), mock.patch("sys.stderr") as err:
                status = premerge.main(["1"])
                shown = "".join(c.args[0] for c in err.write.call_args_list)
        finally:
            os.chdir(cwd)
        return status, shown

    def test_unchanged_refs_pass(self):
        self.assertEqual(self.run_premerge("none")[0], 0)

    def test_a_main_that_moves_during_the_checks_fails(self):
        status, shown = self.run_premerge("main")
        self.assertEqual(status, 1)
        self.assertIn("main moved while checks ran", shown)

    def test_a_pull_request_that_moves_during_the_checks_fails(self):
        status, shown = self.run_premerge("head")
        self.assertEqual(status, 1)
        self.assertIn("#1 changed while checks ran", shown)

    def test_a_non_number_is_refused(self):
        self.assertEqual(premerge.main(["abc"]), 1)

    def test_no_numbers_is_refused(self):
        self.assertEqual(premerge.main([]), 1)


if __name__ == "__main__":
    unittest.main()
