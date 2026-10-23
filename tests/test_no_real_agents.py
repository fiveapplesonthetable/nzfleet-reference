import os
import subprocess
import unittest
from unittest import mock

import no_real_agents  # noqa: F401  (installs the guard for the whole suite, which discovery imports before running anything)
from nzfleet import agents, blocked


class NoRealAgents(unittest.TestCase):
    def test_a_claude_command_cannot_be_launched_by_a_test(self):
        with self.assertRaises(no_real_agents.RealAgentLaunched):
            subprocess.Popen(["timeout", "5", "claude", "-p", "hi"])

    def test_codex_gh_and_docker_cannot_be_launched_by_a_test_either(self):
        for tool in ("codex", "gh", "docker"):
            with self.assertRaises(no_real_agents.RealAgentLaunched):
                subprocess.Popen([tool, "version"])

    def test_other_commands_still_run(self):
        self.assertEqual(subprocess.run(["true"]).returncode, 0)

    def test_an_agent_is_not_started_without_the_live_marker(self):
        with mock.patch.dict(os.environ, clear=False), mock.patch.object(agents.subprocess, "Popen") as popen, \
                mock.patch.object(blocked.mail, "send", return_value=True):
            os.environ.pop(blocked.LIVE, None)
            self.assertIn("not starting", agents.start("fix-2", "a task"))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
