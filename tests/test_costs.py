import unittest

from nzfleet import costs

from snapshots import HOUR, NOW


def record(mid, cost=1.0, at=NOW - 60, place="workers", key="k", model="m"):
    return {"id": mid, "at": at, "model": model, "cost": cost, "place": place, "key": key}


class Costs(unittest.TestCase):
    def test_a_message_logged_twice_is_counted_once(self):
        summary = costs.summary([record("m1"), record("m1")], NOW, NOW - HOUR, {})
        self.assertEqual(summary["hour"], 1.0)

    def test_cache_writes_cost_twice_the_input_rate(self):
        plain = costs.price("claude-opus-4", {"input_tokens": 1_000_000})
        written = costs.price("claude-opus-4", {"cache_creation_input_tokens": 1_000_000})
        self.assertEqual(written, 2 * plain)

    def test_spend_is_split_by_where_it_happened(self):
        summary = costs.summary([record("a", 1.0, place="workers"), record("b", 2.0, place="orchestrator")], NOW, NOW - HOUR, {})
        self.assertEqual((summary["by_place"]["workers"], summary["by_place"]["orchestrator"]), (1.0, 2.0))

    def test_spend_is_attributed_to_the_pr_its_task_produced(self):
        summary = costs.summary([record("a", 3.0, key="k")], NOW, NOW - HOUR, {"k": 42})
        self.assertEqual(summary["per_pr"], {"42": 3.0})


class Projects(unittest.TestCase):
    def test_a_worktree_directory_maps_to_its_task_key(self):
        from nzfleet import snapshot_tick, transcripts
        keys = snapshot_tick.project_keys({"slug": {"path": "/srv/fleet/work/a.b", "agent": "task"}})
        self.assertEqual(transcripts.classify("-mnt-agent-work-a-b/s.jsonl", "-repo", keys), ("workers", "task"))
