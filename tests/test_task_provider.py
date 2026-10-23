import io
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from nzfleet import agents, choice, cli, costs, render, runs, state

from fleet import Fleet, NOW

CODEX = "codex exec --model {model} {brief}"
CLAUDE = "claude -p --model {model} {brief}"


def todo(key="t", model=None, provider=None, compare=None, area=None):
    out, err = io.StringIO(), io.StringIO()
    args = Namespace(key=key, task=["do it"], model=model, provider=provider, compare=compare, area=area, after=None, now=False, retired=False)
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.todo(args)
    return code, out.getvalue(), err.getvalue()


class TaskChoice(Fleet):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(agents, "AGENT_CODEX", CODEX), mock.patch.object(agents, "AGENT_CLAUDE", CLAUDE)):
            patch.start()
            self.addCleanup(patch.stop)

    def start_next(self):
        item = state.read()["todo"][0]
        agents.start(item["key"], item["task"], item["model"], provider_override=item.get("provider"), pinned=bool(item.get("provider")))
        return self.launched[-1][0][2:6]

    def test_a_task_that_names_codex_starts_a_codex_worker_with_its_model(self):
        todo(model="gpt-6-sol")
        self.assertEqual(self.start_next(), ["codex", "exec", "--model", "gpt-6-sol"])

    def test_a_task_that_names_claude_overrides_a_codex_session(self):
        with state.change() as s:
            s["agent_session"] = {"provider": "codex", "model": "gpt-6-luna"}
        todo(provider="claude", model="haiku")
        self.start_next()
        argv = self.launched[-1][0]
        self.assertEqual((argv[2], argv[argv.index("--model") + 1]), ("claude", "haiku"))

    def test_a_task_without_a_choice_follows_the_session(self):
        with state.change() as s:
            s["agent_session"] = {"provider": "codex", "model": "gpt-6-luna"}
        todo()
        self.assertEqual(self.start_next(), ["codex", "exec", "--model", "gpt-6-luna"])

    def test_a_pinned_task_keeps_its_provider_when_it_is_retried_under_another_session(self):
        todo(model="gpt-6-sol")
        self.start_next()
        key = state.read()["todo"][0]["key"]
        self.die(key, NOW)
        with state.change() as s:
            s["agent_session"] = {"provider": "claude", "model": "opus"}
        agents.resume(now=NOW + runs.BACKOFF[0])
        self.assertEqual(self.launched[-1][0][2:6], ["codex", "exec", "--model", "gpt-6-sol"])


class Inference(Fleet):
    def test_a_gpt_model_implies_codex(self):
        self.assertEqual(choice.resolve(None, "gpt-6-luna", "gpt-6-luna"), ("codex", "gpt-6-luna"))

    def test_claude_model_names_imply_claude(self):
        for model in ("sonnet", "haiku", "opus", "claude-sonnet-5-5"):
            self.assertEqual(choice.resolve(None, model, "gpt-6-luna")[0], "claude")

    def test_a_provider_alone_takes_that_providers_default_model(self):
        self.assertEqual(choice.resolve("codex", None, "gpt-6-luna"), ("codex", "gpt-6-luna"))
        self.assertEqual(choice.resolve("claude", None, "gpt-6-luna"), ("claude", "sonnet"))

    def test_no_choice_is_left_to_the_session(self):
        self.assertIsNone(choice.resolve(None, None, "gpt-6-luna"))

    def test_a_model_from_the_other_provider_is_refused(self):
        code, _, err = todo(provider="claude", model="gpt-6-sol")
        self.assertEqual(code, 1)
        self.assertIn("not a claude model", err)
        self.assertEqual(state.read()["todo"], [])

    def test_a_queued_task_without_a_choice_stores_no_provider(self):
        todo()
        self.assertNotIn("provider", state.read()["todo"][0])


class Comparing(Fleet):
    def test_a_comparison_queues_one_task_per_worker_in_lanes_of_their_own(self):
        code, out, _ = todo("kyc", compare="codex:gpt-6-luna,claude:sonnet", area="kyc")
        items = state.read()["todo"]
        self.assertEqual(code, 0)
        self.assertEqual([(i["key"], i["provider"], i["model"], i["area"]) for i in items],
                         [("kyc-gpt-6-luna", "codex", "gpt-6-luna", "kyc-gpt-6-luna"), ("kyc-sonnet", "claude", "sonnet", "kyc-sonnet")])
        self.assertTrue(all("draft" in i["task"] and "do it" in i["task"] for i in items))
        self.assertIn("[gpt-6-luna]", items[0]["task"])

    def test_a_comparison_of_one_worker_is_refused(self):
        code, _, err = todo("kyc", compare="codex:gpt-6-luna")
        self.assertEqual(code, 1)
        self.assertEqual(state.read()["todo"], [])

    def test_a_bare_model_in_a_comparison_infers_its_provider(self):
        self.assertEqual(choice.parse_compare("gpt-6-sol,opus", "gpt-6-luna"), [("codex", "gpt-6-sol"), ("claude", "opus")])


class Showing(Fleet):
    def test_the_queue_listing_shows_provider_and_model(self):
        todo(model="gpt-6-sol")
        out, _ = io.StringIO(), None
        with redirect_stdout(out):
            cli.todo(Namespace(key=None, retired=False))
        self.assertIn("codex/gpt-6-sol", out.getvalue())

    def test_codex_runs_are_counted_apart_from_claude_dollars(self):
        agents_state = {"a": {"provider": "codex", "started": NOW - 60}, "b": {"provider": "claude", "started": NOW - 60},
                        "c": {"provider": "codex", "started": NOW - 7200}}
        self.assertEqual(costs.codex_runs(agents_state, NOW, NOW - 1800), {"codex_hour": 1, "codex_today": 1})

    def test_a_codex_row_shows_no_dollars(self):
        row = {"key": "k", "state": "running", "provider": "codex", "model": "gpt-6-sol", "age": 1, "since_commit": 1, "since_change": 1,
               "cores": 0.0, "turns": 0, "usd": 0.0, "phase": "", "phase_for": 0, "note": ""}
        self.assertIn("codex", render.agents([row])[1].split("turns")[1])
        self.assertNotIn("$0.00", render.agents([row])[1])
