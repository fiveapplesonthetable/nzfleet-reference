import json
import os
import unittest
from unittest import mock

from nzfleet import world


class IssueBodyTransport(unittest.TestCase):
    def test_a_large_fleet_issue_body_is_sent_over_stdin(self):
        arg_max = os.sysconf("SC_ARG_MAX")
        rows = [f"- waiting task {number:08d}: needs owner review" for number in range(arg_max // 32)]
        body = "\n".join(rows)
        self.assertGreater(len(body.encode()), arg_max)
        calls = []

        def run(*args, input=None, **kwargs):
            argv_size = sum(len(arg.encode()) + 1 for arg in args)
            if argv_size > arg_max:
                raise OSError(7, "Argument list too long", args[0])
            calls.append((args, input))
            return ""

        found = {"number": 41, "title": "fleet status", "body": "old"}
        with mock.patch.object(world, "issue", return_value=found), mock.patch.object(world, "run", side_effect=run):
            self.assertEqual(world.set_issue("fleet status", body), 41)

        self.assertEqual(len(calls), 1)
        args, payload = calls[0]
        self.assertIn("--input", args)
        self.assertEqual(json.loads(payload), {"body": body})
        self.assertLess(sum(len(arg.encode()) + 1 for arg in args), arg_max)


if __name__ == "__main__":
    unittest.main()
