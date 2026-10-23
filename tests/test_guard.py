import unittest

from nzfleet import guard


class Refusing(unittest.TestCase):
    def test_a_free_running_yes_is_refused(self):
        for command in ("yes > /dev/null", "timeout 7200 yes >/dev/null &", "for i in $(seq 64); do yes > /dev/null & done",
                        "cd x && /usr/bin/yes", "sudo nice -n 5 yes > /dev/null"):
            self.assertIn("nzfleet starve", guard.refusal(command), command)

    def test_stress_and_stress_ng_are_refused_even_in_a_pipeline(self):
        for command in ("stress --cpu 48", "stress-ng -c 64 --timeout 10m", "echo go; timeout 600 stress-ng -c 8 | tee log"):
            self.assertTrue(guard.refusal(command), command)

    def test_a_command_run_under_nzfleet_starve_is_allowed(self):
        self.assertEqual(guard.refusal("nzfleet starve -- cargo test"), "")
        self.assertEqual(guard.refusal("nzfleet starve --slice 4 -- timeout 60 yes"), "")

    def test_yes_answering_a_prompt_through_a_pipe_is_allowed(self):
        self.assertEqual(guard.refusal("yes | sdkmanager --licenses"), "")

    def test_a_command_that_only_mentions_the_word_is_allowed(self):
        for command in ("echo yes", "grep -rn stress src", "git commit -m 'stress yes'", "cargo test yes_means_yes"):
            self.assertEqual(guard.refusal(command), "", command)


if __name__ == "__main__":
    unittest.main()
