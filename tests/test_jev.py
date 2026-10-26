import datetime
import enum
import json
import tempfile
import unittest

from nzfleet import jev

NOW = datetime.datetime(2026, 10, 16, 20, 36, 41, tzinfo=datetime.timezone.utc)


class Action(enum.Enum):
    RERUN = "rerun"
    BISECT = "bisect"
    DROP = "drop"
    STOP = "stop"


def point(consequence=jev.Consequence.REVERSIBLE, **kw):
    return jev.Point("batch.red_area", Action, consequence, frozenset({"area", "tail"}), **kw)


OPTIONS = [(Action.RERUN, "Rerun the area"), (Action.BISECT, "Bisect the batch"), (Action.DROP, "Drop a member")]


def response(choice, probabilities, confidence=0.9, model="jev-latest"):
    return json.dumps({"model": model, "answers": {"next_action": {
        "type": "choice", "choice": choice, "confidence": confidence, "probabilities": probabilities}}}).encode()


def transport_of(*answers):
    sent = []

    def transport(url, headers, body):
        sent.append((url, dict(headers), json.loads(body)))
        answer = answers[min(len(sent), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer
    transport.sent = sent
    return transport


def typesafe(*answers):
    transport = transport_of(*answers)
    return jev.TypeSafe(transport, key=lambda: "secret-key-value", sleep=lambda _s: None), transport


class Menus(unittest.TestCase):
    def test_a_menu_without_stop_cannot_be_declared(self):
        class NoStop(enum.Enum):
            GO = "go"
        with self.assertRaises(ValueError):
            jev.Point("x", NoStop, jev.Consequence.REVERSIBLE, frozenset())

    def test_duplicate_labels_are_deduplicated_before_the_call(self):
        menu = jev.build_menu(point(), [(Action.RERUN, "Tap 'Transfers'"), (Action.BISECT, "Tap 'Transfers'"), (Action.DROP, "Drop")])
        self.assertEqual([v for v, _ in menu.items], ["rerun", "drop", "stop"])

    def test_stop_is_always_offered(self):
        self.assertEqual(jev.build_menu(point(), OPTIONS).labels["stop"], jev.STOP_LABEL)

    def test_a_member_of_another_enum_is_refused(self):
        class Other(enum.Enum):
            RERUN = "rerun"
        with self.assertRaises(ValueError):
            jev.build_menu(point(), [(Other.RERUN, "Rerun")])


class TypeSafeAnswers(unittest.TestCase):
    PROBS = {"rerun": 0.7, "bisect": 0.1, "drop": 0.1, "stop": 0.1}

    def ask(self, decider):
        return decider.choose({"area": "rust"}, jev.build_menu(point(), OPTIONS), "Pick one.")

    def test_a_valid_answer_is_returned_with_its_confidence(self):
        decider, transport = typesafe(response("rerun", self.PROBS, 0.85))
        verdict = self.ask(decider)
        self.assertEqual((verdict.choice, verdict.confidence, verdict.source), ("rerun", 0.85, "jev"))
        self.assertEqual(transport.sent[0][2]["questions"]["next_action"]["criteria"]["stop"], jev.STOP_LABEL)

    def test_a_response_naming_an_id_not_on_the_menu_is_refused(self):
        decider, _ = typesafe(response("force_push", self.PROBS))
        with self.assertRaises(jev.JevError):
            self.ask(decider)

    def test_probabilities_that_miss_a_menu_id_are_refused(self):
        decider, _ = typesafe(response("rerun", {"rerun": 0.9, "stop": 0.1}))
        with self.assertRaises(jev.JevError):
            self.ask(decider)

    def test_probabilities_not_summing_to_one_are_refused(self):
        decider, _ = typesafe(response("rerun", {k: 0.5 for k in self.PROBS}))
        with self.assertRaises(jev.JevError):
            self.ask(decider)

    def test_a_model_that_is_not_jev_is_refused(self):
        decider, _ = typesafe(response("rerun", self.PROBS, model="gpt-x"))
        with self.assertRaises(jev.JevError):
            self.ask(decider)

    def test_a_429_is_retried_and_the_next_answer_used(self):
        decider, transport = typesafe(jev.Transient("429"), response("rerun", self.PROBS))
        self.assertEqual(self.ask(decider).choice, "rerun")
        self.assertEqual(len(transport.sent), 2)

    def test_three_failures_in_a_row_give_up(self):
        decider, transport = typesafe(jev.Transient("503"))
        with self.assertRaises(jev.JevError):
            self.ask(decider)
        self.assertEqual(len(transport.sent), jev.ATTEMPTS)

    def test_the_key_is_read_once_and_never_in_an_error(self):
        reads = []
        decider = jev.TypeSafe(transport_of(response("rerun", self.PROBS)), key=lambda: reads.append(1) or "k", sleep=lambda _s: None)
        self.ask(decider)
        self.ask(decider)
        self.assertEqual(len(reads), 1)

    def test_a_missing_key_file_is_a_jev_error_without_the_path(self):
        with self.assertRaises(jev.JevError) as caught:
            jev.read_key("/nonexistent/key-file")
        self.assertNotIn("nonexistent", str(caught.exception))


class Deciding(unittest.TestCase):
    PROBS = TypeSafeAnswers.PROBS

    def setUp(self):
        self.log = jev.Log(tempfile.mkdtemp())

    def decide(self, decider, p=None, state=None):
        return jev.decide(p or point(), decider, state or {"area": "rust", "token": "ghp_abcdefghijklmnop"}, OPTIONS, "Pick one.", self.log, NOW)

    def test_a_confident_choice_above_the_floor_is_acted_on(self):
        decider, _ = typesafe(response("bisect", {**self.PROBS, "bisect": 0.7, "rerun": 0.1}, 0.9))
        self.assertEqual(self.decide(decider, point(mode=jev.Mode.ACT)).action, Action.BISECT)

    def test_a_choice_below_the_floor_escalates(self):
        decider, _ = typesafe(response("rerun", self.PROBS, 0.7))
        decision = self.decide(decider, point(jev.Consequence.STOPS_WORK, mode=jev.Mode.ACT))
        self.assertIsNone(decision.action)
        self.assertIn("below the floor", decision.escalation)

    def test_the_same_confidence_passes_a_reversible_floor(self):
        decider, _ = typesafe(response("rerun", self.PROBS, 0.7))
        self.assertEqual(self.decide(decider, point(mode=jev.Mode.ACT)).action, Action.RERUN)

    def test_stop_escalates_however_confident(self):
        decider, _ = typesafe(response("stop", {**self.PROBS, "stop": 0.97, "rerun": 0.01, "bisect": 0.01, "drop": 0.01}, 0.97))
        self.assertEqual(self.decide(decider).escalation, "Jev chose stop")

    def test_an_irreversible_choice_is_never_acted_on_by_jev(self):
        decider, _ = typesafe(response("drop", {**self.PROBS, "drop": 0.9, "rerun": 0.0, "bisect": 0.0, "stop": 0.1}, 0.99))
        decision = self.decide(decider, point(mode=jev.Mode.ACT, irreversible=frozenset({"drop"})))
        self.assertIsNone(decision.action)

    def test_a_jev_failure_escalates_instead_of_choosing(self):
        decider, _ = typesafe(jev.Transient("500"))
        decision = self.decide(decider)
        self.assertEqual((decision.action, decision.record["acted"]), (None, None))

    def test_shadow_acts_on_the_rule_and_logs_what_jev_chose(self):
        decider, _ = typesafe(response("bisect", {**self.PROBS, "bisect": 0.7, "rerun": 0.1}))
        shadow = jev.Shadow(jev.Rule(lambda s, menu: "rerun"), decider)
        decision = self.decide(shadow)
        self.assertEqual((decision.action, decision.record["rule"], decision.record["jev"], decision.record["acted"]), (Action.RERUN, "rerun", "bisect", "rule"))

    def test_shadow_survives_jev_being_down(self):
        decider, _ = typesafe(jev.Transient("500"))
        decision = self.decide(jev.Shadow(jev.Rule(lambda s, menu: "rerun"), decider))
        self.assertEqual((decision.action, decision.record["jev"]), (Action.RERUN, None))

    def test_only_allowed_and_scrubbed_state_leaves_the_machine(self):
        decider, transport = typesafe(response("rerun", self.PROBS))
        self.decide(decider, state={"area": "rust", "tail": "token=abcdef123456", "phone": "+243812345678"})
        self.assertEqual(transport.sent[0][2]["state"], {"area": "rust", "tail": "token=[redacted]"})
        self.assertNotIn("secret-key-value", json.dumps(self.log.read()))

    def test_every_call_is_one_log_line_with_a_fresh_id(self):
        rule = jev.Rule(lambda s, menu: "rerun")
        ids = [self.decide(rule).id for _ in range(2)]
        self.assertEqual(ids, ["d-20261016-203641-0001", "d-20261016-203641-0002"])
        self.assertEqual(len(self.log.read()), 2)


class Replaying(unittest.TestCase):
    def setUp(self):
        self.log = jev.Log(tempfile.mkdtemp())
        self.rule = jev.Rule(lambda s, menu: "bisect" if s.get("area") == "rust" else "rerun")
        self.points = {"batch.red_area": (point(), self.rule)}
        jev.decide(point(), jev.Rule(lambda s, menu: "rerun"), {"area": "rust"}, OPTIONS, "Pick one.", self.log, NOW)

    def test_a_replay_runs_the_current_rule_on_the_logged_state(self):
        out = jev.replay(self.log.read()[0], self.points)
        self.assertEqual((out["logged_rule"], out["rule_now"]), ("rerun", "bisect"))

    def test_a_replay_of_an_unregistered_point_is_refused(self):
        with self.assertRaises(jev.JevError):
            jev.replay(self.log.read()[0], {})

    def test_live_replay_asks_jev_with_the_logged_state(self):
        decider, transport = typesafe(response("drop", {"rerun": 0.0, "bisect": 0.1, "drop": 0.8, "stop": 0.1}))
        self.assertEqual(jev.replay(self.log.read()[0], self.points, decider)["jev_now"], "drop")
        self.assertEqual(transport.sent[0][2]["state"], {"area": "rust"})


class Reporting(unittest.TestCase):
    def test_the_report_counts_agreement_floor_misses_stops_and_outcomes(self):
        log = jev.Log(tempfile.mkdtemp())
        probs = TypeSafeAnswers.PROBS
        for answer in (response("rerun", probs, 0.9), response("bisect", {**probs, "bisect": 0.7, "rerun": 0.1}, 0.3), response("stop", {**probs, "stop": 0.7, "rerun": 0.1}, 0.9)):
            decider, _ = typesafe(answer)
            jev.decide(point(), jev.Shadow(jev.Rule(lambda s, menu: "rerun"), decider), {}, OPTIONS, "Pick one.", log, NOW)
        self.assertTrue(log.set_outcome("d-20261016-203641-0001", "ok"))
        self.assertFalse(log.set_outcome("d-missing", "ok"))
        row = jev.summarise(log.read())["batch.red_area"]
        self.assertEqual((row["calls"], row["compared"], row["agreed"], row["floor_misses"], row["stops"], row["outcomes"]),
                         (3, 3, 1, 1, 1, {"rule": {"ok": 1}}))

    def test_a_torn_line_is_skipped_and_the_rest_still_read(self):
        log = jev.Log(tempfile.mkdtemp())
        log.append({"id": "d-1", "point": "batch.red_area"})
        with open(log.path, "a", encoding="utf-8") as f:
            f.write("}}\n")
        log.append({"id": "d-2", "point": "batch.red_area"})
        self.assertEqual([r["id"] for r in log.read()], ["d-1", "d-2"])
        self.assertTrue(log.set_outcome("d-2", "ok"))
        self.assertEqual(log.read()[1]["outcome"], "ok")

    def test_an_empty_log_says_so(self):
        self.assertEqual(jev.format_report({}), "no decisions logged")
