import json
import unittest
from unittest import mock

from nzfleet import render, selfupdate, snapshot

from snapshots import HOUR, NOW, observed, proc, running_agent, taken, with_state


def kinds(shot, section="attention"):
    return [line["kind"] for line in shot[section]]


class TheSnapshot(unittest.TestCase):
    def test_an_invalid_load_limit_is_visible_in_attention(self):
        with mock.patch("nzfleet.config.CONFIG_PROBLEM", "bad load limit"):
            [line] = taken()["attention"]
        self.assertEqual((line["kind"], line["subject"], line["text"]),
                         ("config", "CPU limit", "bad load limit"))

    def test_an_unresolved_postsubmit_failure_is_visible_in_attention(self):
        [line] = taken(postsubmit_issue={"number": 42})["attention"]
        self.assertEqual((line["kind"], line["subject"], line["fix"]),
                         ("postsubmit", "issue #42", "review issue #42"))

    def test_a_burner_left_running_shows_under_needs_attention(self):
        shot = taken(procs=[proc(500, "yes", age=1200.0, cwd="/srv/fleet/work/x")])
        [line] = shot["attention"]
        self.assertEqual((line["kind"], line["fix"]), ("burner", "kill -TERM 500"))

    def test_a_burner_under_nzfleet_starve_is_not_flagged(self):
        self.assertEqual(taken(procs=[proc(500, "yes", starved=True)])["attention"], [])

    def test_a_running_batch_with_a_merged_pr_is_flagged(self):
        s = with_state(merged_at={"7": NOW - HOUR}, batches={"b1": {"status": "running", "members": [[7, "abc"]], "started": NOW - 60, "pid": 31}})
        self.assertEqual(kinds(taken(state=s)), ["merged in batch"])

    def test_code_behind_main_for_over_half_an_hour_is_flagged(self):
        behind = (selfupdate.Behind("abc", NOW - 40 * 60),)
        [line] = taken(behind=behind)["attention"]
        self.assertEqual((line["kind"], line["fix"]), ("code", "nzfleet version"))

    def test_code_behind_main_for_a_few_minutes_is_not(self):
        self.assertEqual(taken(behind=(selfupdate.Behind("abc", NOW - 5 * 60),))["attention"], [])

    def test_a_hold_older_than_a_day_is_flagged_with_its_release_command(self):
        s = with_state(holds={"old": {"why": "waits", "since": NOW - 25 * HOUR}, "new": {"why": "waits", "since": NOW - HOUR}})
        [line] = taken(state=s)["attention"]
        self.assertEqual(line["fix"], "nzfleet release old")

    def test_the_held_section_marks_the_stale_ones(self):
        s = with_state(holds={"old": {"why": "waits", "since": NOW - 25 * HOUR}, "new": {"why": "waits", "since": NOW - HOUR}})
        self.assertEqual({h["key"]: h["stale"] for h in taken(state=s)["held"]["holds"]}, {"old": True, "new": False})

    def test_a_verified_train_puts_the_merge_command_under_needs_you(self):
        s = with_state(notified=[5], notified_trains=["t1"],
                       trains={"t1": {"id": "t1", "status": "open", "members": [[5, "abc"]]}})
        [line] = taken(state=s)["needs_you"]
        self.assertEqual(line["kind"], "merge")

    def test_an_agent_that_wrote_nothing_for_twenty_minutes_is_stuck(self):
        s = with_state(agents={"app-data": running_agent(started=NOW - HOUR)})
        self.assertEqual(kinds(taken(state=s, agent_pids={"app-data": 900})), ["stuck"])

    def test_an_agent_still_using_cpu_is_not_stuck(self):
        s = with_state(agents={"app-data": running_agent()}, samples={"app-data": {"cpu": 5.0, "at": NOW, "alive_at": NOW, "busy_at": NOW - 60}})
        self.assertEqual(taken(state=s, agent_pids={"app-data": 900})["attention"], [])

    def test_agent_status_reports_the_provider_and_model_recorded_at_dispatch(self):
        agent = {**running_agent(), "provider": "codex", "model": "gpt-6-luna"}
        row, = taken(state=with_state(agents={"app-data": agent}))["agents"]
        self.assertEqual((row["provider"], row["model"]), ("codex", "gpt-6-luna"))

    def test_a_spend_over_the_limit_is_flagged(self):
        s = with_state(agents={"app-data": running_agent()}, samples={"app-data": {"cpu": 5.0, "at": NOW, "alive_at": NOW, "busy_at": NOW}})
        records = [{"id": "m", "at": NOW - 60, "model": "claude-opus-4", "cost": 20.0, "place": "workers", "key": "app-data"}]
        self.assertEqual(kinds(taken(state=s, cost_records=records)), ["budget"])

    def test_the_headline_counts_what_needs_a_person_and_an_agent(self):
        shot = taken(procs=[proc(500, "yes", age=1200.0)])
        self.assertEqual((shot["headline"]["needs_you"], shot["headline"]["attention"]), (0, 1))

    def test_a_pending_main_run_does_not_inherit_the_previous_failure(self):
        s = with_state(main_ci={"sha": "old", "ok": False, "since": NOW - 600, "completed_at": NOW - 600},
                       main_ci_current={"sha": "new", "status": "pending", "conclusion": "pending", "run_id": 4, "at": NOW - 5})
        main = taken(state=s)["pipeline"]["main"]
        self.assertEqual((main["sha"], main["status"], main["green"], main["last_completed_sha"], main["last_completed_green"]),
                         ("new", "pending", None, "old", False))
        line = render.pipeline({"main": main, "queue": "", "batches": [], "train_prs": [], "chains": [], "waiting": [], "train_refused": ""})[2]
        self.assertIn("main new CI: pending run #4", line)
        self.assertIn("last completed old RED 10 min ago", line)
        self.assertNotIn("deployed", line)

    def test_a_pending_run_after_green_keeps_the_completed_result_as_history(self):
        s = with_state(main_ci={"sha": "old", "ok": True, "since": NOW - 3600, "completed_at": NOW - 120},
                       main_ci_current={"sha": "new", "status": "queued", "conclusion": "pending", "run_id": 5, "first_observed_at": NOW - 120, "at": NOW})
        main = taken(state=s)["pipeline"]["main"]
        self.assertIsNone(main["green"])
        self.assertEqual((main["last_completed_green"], main["last_completed_for"], main["for"]), (True, 120, 120))
        line = render.pipeline({"main": main, "queue": "", "batches": [], "train_prs": [], "chains": [], "waiting": [], "train_refused": ""})[2]
        self.assertIn("main new CI: queued run #5", line)
        self.assertIn("last completed old green 2 min ago", line)

    def test_unknown_main_api_state_stays_unknown(self):
        s = with_state(main_ci={"sha": "old", "ok": True, "since": NOW - 120})
        main = taken(state=s)["pipeline"]["main"]
        self.assertEqual((main["status"], main["green"], main["last_completed_green"]), ("unknown", None, True))

    def test_legacy_completion_without_a_timestamp_does_not_use_the_outcome_interval_as_age(self):
        s = with_state(main_ci={"sha": "old", "ok": True, "since": NOW - 3600})
        main = taken(state=s)["pipeline"]["main"]
        self.assertIsNone(main["last_completed_for"])
        line = render.pipeline({"main": main, "queue": "", "batches": [], "train_prs": [], "chains": [], "waiting": [], "train_refused": ""})[2]
        self.assertIn("last completed old green age unknown", line)

    def test_latest_timed_out_run_is_shown_as_red_for_its_sha(self):
        s = with_state(main_ci={"sha": "latest", "run_id": 9, "ok": False, "since": NOW - 600, "completed_at": NOW - 10},
                       main_ci_current={"sha": "latest", "status": "completed", "conclusion": "failure", "run_id": 9,
                                        "first_observed_at": NOW - 10, "at": NOW})
        main = taken(state=s)["pipeline"]["main"]
        self.assertEqual((main["sha"], main["status"], main["green"]), ("latest", "completed", False))
        line = render.pipeline({"main": main, "queue": "", "batches": [], "train_prs": [], "chains": [], "waiting": [], "train_refused": ""})[2]
        self.assertIn("main latest CI: completed run #9 (RED)", line)

    def test_it_survives_a_round_trip_through_json(self):
        shot = taken(procs=[proc(500, "yes", age=1200.0)])
        self.assertEqual(json.loads(json.dumps(shot)), shot)

    def test_a_section_is_picked_by_name_or_alias(self):
        shot = taken()
        self.assertEqual(list(snapshot.section(shot, ["needs"])), ["needs_you", "attention"])
        self.assertEqual(list(snapshot.section(shot, ["cost"])), ["cost"])

    def test_cpu_by_owner_reports_the_quota_beside_the_use(self):
        shot = taken(quotas={"batches": 24.0})
        self.assertEqual(shot["machine"]["cpu_by_owner"]["batches"], {"cores": 0.0, "quota": 24.0})

    def test_ci_container_processes_are_counted_once_after_the_runner_root_exits(self):
        from nzfleet import owners

        scope = "/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope"
        rust = proc(502, "rustc", ppid=1, cpu=40.0, age=100.0, cwd="/__w/nzela/nzela", cgroup=scope)
        java = proc(504, "java", ppid=1, cpu=20.0, age=100.0, cwd="/tmp", cgroup=scope)
        unrelated = proc(503, "cargo", ppid=1, cpu=100.0, age=100.0, cwd="/workspace/other", cgroup="/user.slice/user-1000.slice/session-1.scope")
        owner_of = owners.classify([rust, java, unrelated], {}, "/repo")
        self.assertEqual((owner_of[502], owner_of[504], owner_of[503]),
                         (("ci_runners", ""), ("ci_runners", ""), ("other", "")))
        used, _, _ = owners.rates([rust, java, unrelated], owner_of, {"502": 10.0, "504": 10.0}, 10.0)
        self.assertEqual((used["ci_runners"], used["other"]), (4.0, 1.0))

    def test_a_build_process_in_an_unidentified_docker_scope_stays_unknown(self):
        from nzfleet import owners

        scope = "/system.slice/docker-fedcba9876543210.scope"
        rust = proc(505, "cargo", ppid=1, cpu=30.0, age=10.0, cwd="/build", cgroup=scope)
        owner_of = owners.classify([rust], {}, "/repo")
        used, _, _ = owners.rates([rust], owner_of, {}, 10.0)
        self.assertEqual((owner_of[505], used["ci_runners"], used["unknown"]), (("unknown", ""), 0.0, 3.0))

    def test_a_nested_workspace_prefix_is_not_ci_evidence(self):
        from nzfleet import owners

        scope = "/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope"
        rust = proc(506, "rustc", ppid=1, cpu=40.0, age=100.0, cwd="/__w/nzela/nzelax", cgroup=scope)
        owner_of = owners.classify([rust], {}, "/repo")
        self.assertEqual(owner_of[506], ("unknown", ""))

    def test_a_native_build_outside_ci_workspace_stays_other(self):
        from nzfleet import owners

        rust = proc(507, "cargo", ppid=1, cpu=40.0, age=100.0, cwd="/work/native-build", cgroup="/user.slice/user-1000.slice/session-1.scope")
        owner_of = owners.classify([rust], {}, "/repo")
        self.assertEqual(owner_of[507], ("other", ""))

    def test_agent_and_burner_ownership_keep_their_existing_precedence(self):
        from nzfleet import owners

        root = proc(600, "claude")
        agent_build = proc(601, "cargo", ppid=600, cwd="/work", cgroup="/system.slice/docker-abc.scope")
        burner = proc(602, "yes", ppid=600, cwd="/work", cgroup="/system.slice/docker-abc.scope")
        owner_of = owners.classify([root, agent_build, burner], {"task": 600}, "/repo")
        self.assertEqual((owner_of[601], owner_of[602]), (("agents", "task"), ("burners", "")))

    def test_the_next_snapshot_rates_cpu_from_the_last_ones_seconds(self):
        busy = proc(600, "cargo", cpu=130.0, age=500.0, cgroup="/user.slice/nzf-1.scope")
        first = taken(procs=[proc(600, "cargo", cpu=100.0, age=470.0, cgroup=busy.cgroup)], now=NOW - 30)
        second = snapshot.snapshot(observed(procs=[busy], previous=json.loads(json.dumps(first))))
        self.assertEqual(second["machine"]["cpu_by_owner"]["batches"]["cores"], 1.0)


class Retries(unittest.TestCase):
    def test_a_second_run_is_normal_and_a_third_is_flagged(self):
        second = with_state(agents={"k": running_agent(), "k~2": running_agent()})
        third = with_state(agents={"k": running_agent(), "k~2": running_agent(), "k~3": running_agent()})
        self.assertEqual((kinds(taken(state=second)).count("retried"), kinds(taken(state=third)).count("retried")), (0, 1))

    def test_a_retired_run_no_longer_needs_attention(self):
        retired = with_state(agents={"k": {**running_agent(), "status": "retired"},
                                     "k~2": {**running_agent(), "status": "retired"},
                                     "k~3": {**running_agent(), "status": "retired"}})
        self.assertEqual(kinds(taken(state=retired)).count("retried"), 0)
