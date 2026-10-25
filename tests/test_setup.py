import os
import re
import tempfile
import unittest
from unittest import mock

from nzfleet import config, setup

ALL_GOOD = {"docker": (0, ""), "rustup": (0, "1.98-x86_64\n"), "node": (0, "v22.4.0\n")}


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class FakeTools:
    """Answers every command with success unless the table says otherwise; remembers what was run."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, argv, env=None):
        self.calls.append(tuple(argv))
        for prefix, answer in self.answers.items():
            if " ".join(argv).startswith(prefix):
                return answer
        if argv[0] == "rustup":
            return 0, "stable\n1.98-x86_64-unknown-linux-gnu\n"
        if argv[0] == "node":
            return 0, "v22.4.0\n"
        if argv[0] == "sh":
            return 0, "/somewhere/bin/sccache\n"
        if argv[:2] == ("loginctl", "show-user"):
            return 0, "Linger=yes\n"
        if argv[0] == "git" and "worktree" in argv and "add" in argv:
            os.makedirs(argv[-2], exist_ok=True)
        return 0, ""


class Setup(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.out = []
        self.patches = [mock.patch.dict(os.environ, {"NZFLEET_ENV": os.path.join(self.home, "env"), "USER": "tester"}),
                        mock.patch.object(config, "REPO", os.path.join(self.home, "nzela")),
                        mock.patch.object(config, "ANDROID_HOME", self.home),
                        mock.patch.object(config, "HOME", os.path.join(self.home, "nzfleet-home")),
                        mock.patch.object(config, "WARM", os.path.join(self.home, "nzfleet-home", "warm")),
                        mock.patch.object(config, "SCCACHE", os.path.join(self.home, "nzfleet-home", "sccache")),
                        mock.patch.object(config, "CACHE", os.path.join(self.home, "nzfleet-home", "cache"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.home, "platforms"))
        os.makedirs(os.path.join(self.home, "nzela", "tools", "nzfleet", "runner"))
        write(os.path.join(self.home, "nzela", "tools", "nzfleet", "runner", "install-sccache.sh"), "")

    def go(self, tools=None):
        self.tools = tools or FakeTools()
        code = setup.setup(self.home, self.tools, self.out.append)
        return code, "\n".join(self.out)

    def worktree_made(self):
        os.makedirs(os.path.join(self.home, "nzfleet-main", "tools", "nzfleet", "bin"))

    def test_a_fresh_machine_gets_an_env_file_with_its_own_paths_filled_in(self):
        self.go()
        text = read(os.path.join(self.home, "env"))
        self.assertIn(f"NZFLEET_REPO={self.home}/nzela", text)
        self.assertEqual(re.findall(r"@[A-Z]+@", text), [])

    def test_an_env_file_the_owner_edited_is_never_overwritten(self):
        path = os.path.join(self.home, "env")
        write(path, "NZFLEET_REPO=/mine\n")
        self.go()
        self.assertEqual(read(path), "NZFLEET_REPO=/mine\n")

    def test_a_kept_env_file_is_asked_about_what_it_never_mentions(self):
        write(os.path.join(self.home, "env"), "NZFLEET_REPO=/mine\n")
        _, said = self.go()
        self.assertIn("NZFLEET_AGENT_SLOTS", said)

    def test_the_units_are_installed_once_and_a_second_run_changes_nothing(self):
        self.go()
        first = [c for c in self.tools.calls if c[:3] == ("systemctl", "--user", "daemon-reload")]
        self.out.clear()
        self.go()
        second = [c for c in self.tools.calls if c[:3] == ("systemctl", "--user", "daemon-reload")]
        self.assertEqual((len(first), len(second), "installed" in "\n".join(self.out)), (2, 0, False))
        self.assertIn("Machine tidy, including idle build output, runs through nzfleet every 15 minutes.", "\n".join(self.out))

    def test_a_changed_unit_is_refreshed(self):
        self.go()
        unit = os.path.join(self.home, ".config", "systemd", "user", "nzfleet.service")
        write(unit, "stale\n")
        _, said = self.go()
        self.assertIn("installed nzfleet.service", said)
        self.assertNotIn("stale", read(unit))

    def test_every_missing_prerequisite_is_named_with_what_to_do(self):
        tools = FakeTools({"docker info": (1, ""), "rustup": (0, "stable\n"), "node": (0, "v18.1.0\n"), "gh auth": (1, "")})
        code, said = self.go(tools)
        self.assertEqual(code, 1)
        for name in ("docker", "rustup 1.98", "node 22", "gh signed in"):
            self.assertIn(f"missing: {name}", said)
        self.assertNotIn("missing: Android SDK", said)

    def test_a_checkout_without_the_hooks_is_reported(self):
        code, said = self.go(FakeTools({"test -x": (1, "")}))
        self.assertEqual(code, 1)
        self.assertIn("missing: hook bash-guard.py", said)

    def test_a_machine_with_everything_exits_zero(self):
        code, said = self.go()
        self.assertEqual(code, 0)
        self.assertNotIn("missing:", said)

    def test_the_directories_batches_reuse_are_made_once(self):
        self.go()
        for path in (config.WARM, config.SCCACHE, os.path.join(config.CACHE, "npm"), os.path.join(config.CACHE, "cargo-registry")):
            self.assertTrue(os.path.isdir(path), path)
        self.out.clear()
        _, again = self.go()
        self.assertNotIn("made ", again)

    def test_sccache_is_installed_by_the_repositorys_script_into_the_homes_bin(self):
        self.go()
        install = [c for c in self.tools.calls if c[0] == "sh"][0]
        self.assertEqual(install[-2:], (os.path.join(self.home, "nzela", "tools", "nzfleet", "runner", "install-sccache.sh"),
                                        os.path.join(config.HOME, "bin")))

    def test_an_sccache_that_cannot_be_installed_is_named_and_fails_setup(self):
        code, said = self.go(FakeTools({"sh": (0, "")}))
        self.assertEqual(code, 1)
        self.assertIn("missing: sccache could not be installed", said)

    def test_the_one_time_steps_for_the_ci_runners_are_printed_and_not_run(self):
        _, said = self.go()
        self.assertIn('provision --repository-id "$(gh api repos/nzela-org/nzela --jq .id)" --owner-uid 1001', said)
        self.assertIn("owner UID 1002", said)
        self.assertIn("configured CI_WARM_DIR takes precedence", said)
        self.assertFalse([c for c in self.tools.calls if c[0] == "sudo" or c[:2] == ("gh", "api")])

    def runner_account(self, token=""):
        os.makedirs(os.path.join(self.home, "actions-runner"))
        hook = os.path.join(self.home, "nzela", "tools", "nzfleet", "runner", "runner-job-started.sh")
        write(hook, "#!/bin/sh\n")
        with mock.patch.dict(config.FILE, {"NZFLEET_MIRROR_TOKEN_FILE": token}):
            return self.go()

    def test_a_runner_account_gets_the_hook_and_the_mirror_it_refreshes_from_the_repository(self):
        _, said = self.runner_account("/secrets/mirror")
        self.assertEqual(read(os.path.join(self.home, "actions-runner", "job-started.sh")), "#!/bin/sh\n")
        dropin = read(os.path.join(self.home, ".config", "systemd", "user", "nzela-runner@.service.d", "60-git-mirror.conf"))
        self.assertIn("NZELA_GIT_MIRROR=%h/git-mirror/repo.git", dropin)
        self.assertIn("NZELA_GIT_MIRROR_TOKEN_FILE=/secrets/mirror", dropin)
        self.assertIn("job-started.sh; restart the runners", said)

    def test_a_second_setup_leaves_the_hook_and_mirror_alone(self):
        self.runner_account()
        self.out.clear()
        _, again = self.go()
        self.assertNotIn("job-started.sh", again)

    def test_an_account_without_runners_gets_no_hook(self):
        write(os.path.join(self.home, "nzela", "tools", "nzfleet", "runner", "runner-job-started.sh"), "#!/bin/sh\n")
        self.go()
        self.assertFalse(os.path.exists(os.path.join(self.home, "actions-runner")))

    def test_the_root_crontab_is_printed_and_never_touched(self):
        _, said = self.go()
        self.assertIn("7 * * * * timeout 10m flock -n /run/lock/nzela-runner-prune.lock /usr/local/sbin/nzela-runner-prune scheduled", said)
        self.assertFalse([c for c in self.tools.calls if "crontab" in c[0] or c[0] == "sudo"])

    def test_two_prunes_cannot_run_at_once(self):
        self.assertIn("flock -n /run/lock/nzela-runner-prune.lock", setup.ROOT_LINE)
        self.assertIn("timeout 10m flock", setup.ROOT_LINE)

    def test_commands_are_linked_into_the_users_bin_without_replacing_a_file_there(self):
        self.worktree_made()
        taken = os.path.join(self.home, ".local", "bin", "agent-db")
        os.makedirs(os.path.dirname(taken))
        write(taken, "mine")
        _, said = self.go()
        self.assertTrue(os.path.islink(os.path.join(self.home, ".local", "bin", "nzfleet")))
        self.assertEqual(read(taken), "mine")
        self.assertIn(f"{taken} exists and is not a link", said)


class Template(unittest.TestCase):
    TEXT = read(os.path.join(setup.BIN, "env.template"))

    def test_it_explains_every_variable_the_code_reads(self):
        source = read(os.path.join(os.path.dirname(setup.BIN), "nzfleet", "config.py"))
        used = set(re.findall(r'_get\("([A-Z_]+)"', source)) - {"NZFLEET_FORK"}
        documented = set(re.findall(r"^#?([A-Z_]+)=", self.TEXT, re.M))
        self.assertEqual(used - documented, set())

    def test_it_holds_no_secret_only_unset_names(self):
        values = [line.partition("=")[2] for line in self.TEXT.splitlines() if line[:1].isupper()]
        self.assertTrue(all(v.startswith("@") or v.startswith("/") or not v for v in values))

    def test_a_variable_that_is_documented_but_absent_from_an_env_file_is_reported(self):
        self.assertIn("NZFLEET_PSQL", setup.missing_variables(self.TEXT, "NZFLEET_HOME=/x\n"))
        self.assertNotIn("NZFLEET_PSQL", setup.missing_variables(self.TEXT, "#NZFLEET_PSQL=\n"))


if __name__ == "__main__":
    unittest.main()


POOLS = "docker info --format"


class AddressPools(Setup):
    def test_the_default_pools_get_the_exact_daemon_json_and_the_warning_about_the_restart(self):
        _, said = self.go(FakeTools({POOLS: (0, "null\n")}))
        self.assertIn('"default-address-pools": [{"base": "10.210.0.0/16", "size": 24}]', said)
        self.assertIn("/etc/docker/daemon.json", said)
        self.assertIn("restarts docker", said)
        self.assertIn("no batch or agent database is running", said)

    def test_pools_the_owner_already_set_are_left_unmentioned(self):
        _, said = self.go(FakeTools({POOLS: (0, '[{"Base":"10.210.0.0/16","Size":24}]\n')}))
        self.assertNotIn("default-address-pools", said)

    def test_setup_never_changes_the_daemon_and_the_exit_code_is_unaffected(self):
        code, _ = self.go(FakeTools({POOLS: (0, "null\n")}))
        self.assertEqual(code, 0)
        self.assertFalse([c for c in self.tools.calls if "systemctl" in c[0] and "docker" in " ".join(c)])

    def test_a_daemon_that_cannot_be_asked_adds_nothing(self):
        _, said = self.go(FakeTools({POOLS: (1, "")}))
        self.assertNotIn("default-address-pools", said)
