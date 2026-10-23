import os
import tempfile
import time
import unittest
from unittest import mock

import no_real_agents  # noqa: F401
from nzfleet import agents, blocked, cli, config, mail, render, snapshot, state, units

from snapshots import NOW, taken, with_state


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class InstalledUnitsAreComparedWithTheShippedOnes(unittest.TestCase):
    def setUp(self):
        self.home, self.bin = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.reloads = []
        self.installed = os.path.join(units.installed_dir(self.home), units.SERVICE)
        write(os.path.join(self.bin, units.SERVICE), "new\n")

    def sync(self):
        return units.sync(self.home, self.bin, lambda: self.reloads.append(1))

    def test_an_older_installed_unit_is_replaced_and_systemd_reloaded(self):
        write(self.installed, "old\n")
        self.assertEqual(self.sync(), (units.SERVICE,))
        with open(self.installed) as f:
            self.assertEqual((f.read(), self.reloads), ("new\n", [1]))

    def test_a_missing_unit_is_installed(self):
        self.assertEqual(self.sync(), (units.SERVICE,))

    def test_a_matching_unit_is_left_alone_and_nothing_is_reloaded(self):
        write(self.installed, "new\n")
        self.assertEqual((self.sync(), self.reloads), ((), []))

    def test_the_shipped_service_unit_grants_the_licence_to_start_agents(self):
        with open(os.path.join(units.BIN, units.SERVICE)) as f:
            self.assertIn(f"Environment={blocked.LIVE}=1", f.read())


class ADaemonThatCannotStartAgentsSaysSo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.mails = []
        for patch in (mock.patch.object(state, "PATH", os.path.join(self.dir, "state.json")),
                      mock.patch.object(state, "LOCK", os.path.join(self.dir, "state.lock")),
                      mock.patch.object(config, "AGENT_SLOTS", 4),
                      mock.patch.object(mail, "send", side_effect=lambda subject, body: self.mails.append((subject, body)) or True),
                      mock.patch.dict(os.environ, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(blocked.LIVE, None)

    def refuse(self, key="fix-1"):
        with mock.patch.object(agents.subprocess, "Popen") as popen:
            agents.start(key, "a task")
        popen.assert_not_called()

    def test_the_refusal_puts_a_red_line_first_in_status(self):
        self.refuse()
        shot = taken(state=state.read(), now=time.time())
        first = render.text(shot).splitlines()[0]
        self.assertTrue(first.startswith(f"daemon cannot start agents: {blocked.LIVE} is not set"), first)
        self.assertIn(", since ", first)

    def test_status_has_no_such_line_while_agents_can_start(self):
        self.assertNotIn("cannot start", render.text(taken()))

    def test_the_orchestrator_is_emailed_once_however_many_tasks_are_refused(self):
        self.refuse("fix-1")
        self.refuse("fix-2")
        self.assertEqual(len(self.mails), 1)
        self.assertIn(blocked.LIVE, self.mails[0][1])

    def test_a_mail_that_failed_is_tried_again_on_the_next_refusal(self):
        with mock.patch.object(mail, "send", return_value=False) as failing:
            self.refuse("fix-1")
        self.refuse("fix-2")
        self.assertEqual((failing.call_count, len(self.mails)), (1, 1))

    def test_the_time_it_began_survives_later_refusals(self):
        self.refuse("fix-1")
        since = state.read()["blocked"]["since"]
        with mock.patch.object(time, "time", return_value=since + 600):
            self.refuse("fix-2")
        self.assertEqual(state.read()["blocked"]["since"], since)

    def test_starting_an_agent_clears_it(self):
        self.refuse()
        os.environ[blocked.LIVE] = "1"
        with mock.patch.object(agents.subprocess, "Popen", return_value=mock.Mock(pid=1)), mock.patch.object(agents, "COMMAND", "true"), \
                mock.patch.object(agents, "open", mock.mock_open(), create=True):
            agents.start("fix-3", "a task")
        self.assertEqual(state.read()["blocked"], {})


class TheDaemonChecksItsLicenceAtStartup(unittest.TestCase):
    def run_daemon(self, env, changed=()):
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(units, "sync", return_value=changed), \
                mock.patch.object(cli.codehost, "restart_service") as restart, mock.patch.object(cli.time, "sleep", side_effect=AssertionError("idled")), \
                mock.patch.object(cli, "poll", side_effect=AssertionError("polled")), mock.patch("sys.stderr") as err:
            code = cli.daemon(None)
        return code, restart.call_count, "".join(c.args[0] for c in err.write.call_args_list)

    def test_without_the_marker_it_exits_non_zero_with_the_reason_instead_of_idling(self):
        code, restarts, said = self.run_daemon({})
        self.assertEqual((code, restarts), (1, 0))
        self.assertIn(f"daemon cannot start agents: {blocked.LIVE} is not set", said)

    def test_a_service_unit_it_just_reinstalled_is_applied_by_a_systemd_restart_not_a_failure(self):
        code, restarts, _ = self.run_daemon({}, changed=(units.SERVICE,))
        self.assertEqual((code, restarts), (0, 1))


if __name__ == "__main__":
    unittest.main()
