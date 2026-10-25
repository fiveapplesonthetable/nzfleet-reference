"""What main believes about a pull request run, decided from plain data."""

import io
import json
import unittest
import zipfile

from nzfleet.selection import proof as ci_proof

TREE = "a" * 40
BASE = "b" * 40
HEAD = "c" * 40
IMAGE = "sha256:" + "d" * 64
LABELS = ["nzela-pr"]


def proof(**changed):
    return {"tree": TREE, "base": BASE, "areas": ["e2e", "rust"], "image": IMAGE, **changed}


def run(**changed):
    made = {
        "id": 7,
        "head_sha": HEAD,
        "event": "pull_request",
        "conclusion": "success",
        "path": ".github/workflows/ci.yaml@refs/pull/9/merge",
        "jobs": [{"conclusion": "success", "labels": ["nzela-pr"]}, {"conclusion": "skipped", "labels": ["ubuntu-latest"]}],
        "proof": proof(),
    }
    return {**made, **changed}


def believed(*runs, tree=TREE, heads=(HEAD,), labels=LABELS):
    return ci_proof.choose({"tree": tree, "heads": list(heads), "labels": labels, "runs": list(runs)})


class Choose(unittest.TestCase):
    def test_a_green_pull_request_run_of_this_tree_is_believed(self):
        self.assertEqual(believed(run()), {"run": 7, "base": BASE, "areas": ["e2e", "rust"], "image": IMAGE})

    def test_a_different_tree_is_not_believed(self):
        self.assertEqual(believed(run(), tree="e" * 40), {})

    def test_a_run_that_did_not_succeed_is_not_believed(self):
        self.assertEqual(believed(run(conclusion="failure")), {})

    def test_a_push_run_is_not_believed(self):
        self.assertEqual(believed(run(event="push")), {})

    def test_another_workflow_is_not_believed(self):
        self.assertEqual(believed(run(path=".github/workflows/other.yaml@refs/pull/9/merge")), {})

    def test_a_head_that_never_reached_main_is_not_believed(self):
        self.assertEqual(believed(run(head_sha="f" * 40)), {})

    def test_a_job_on_another_machine_is_not_believed(self):
        stranger = {"conclusion": "success", "labels": ["ubuntu-latest"]}
        self.assertEqual(believed(run(jobs=[stranger])), {})

    def test_a_run_with_no_job_that_ran_is_not_believed(self):
        self.assertEqual(believed(run(jobs=[{"conclusion": "skipped", "labels": []}])), {})

    def test_a_run_without_a_proof_is_not_believed(self):
        self.assertEqual(believed(run(proof=None)), {})

    def test_a_proof_for_another_tree_is_not_believed(self):
        self.assertEqual(believed(run(proof=proof(tree="e" * 40))), {})

    def test_a_proof_with_an_unknown_shape_is_not_believed(self):
        for bad in (proof(areas="rust"), proof(areas=["rust", 3]), proof(base="main"), proof(image="latest")):
            self.assertEqual(believed(run(proof=bad)), {}, bad)

    def test_a_proof_without_an_image_names_none(self):
        self.assertEqual(believed(run(proof=proof(image="")))["image"], "")

    def test_the_newest_believable_run_wins(self):
        newer = run(id=9, proof=proof(areas=["rust"]))
        self.assertEqual(believed(run(), newer, run(id=11, conclusion="failure"))["run"], 9)

    def test_a_parent_of_the_merge_commit_counts_as_the_head(self):
        self.assertEqual(believed(run(), heads=("1" * 40, HEAD))["run"], 7)


def skipped(*runs, tree=TREE, moved=()):
    """What main skips for `tree`: the areas of the run it believes that nothing has moved under."""
    found = believed(*runs, tree=tree)
    return ci_proof.proved(found.get("areas", []), list(moved), found.get("image", ""))


class Skipped(unittest.TestCase):
    def test_a_tree_equal_to_a_passed_run_skips_what_that_run_ran(self):
        self.assertEqual(skipped(run()), ["e2e", "image", "rust"])

    def test_a_different_tree_skips_nothing(self):
        self.assertEqual(skipped(run(), tree="e" * 40), [])

    def test_a_failed_run_skips_nothing(self):
        self.assertEqual(skipped(run(conclusion="failure")), [])

    def test_a_run_still_going_skips_nothing(self):
        self.assertEqual(skipped(run(conclusion=None)), [])

    def test_an_area_the_run_did_not_run_is_not_skipped(self):
        self.assertNotIn("console", skipped(run()))

    def test_an_area_that_moved_before_the_run_branched_is_run_again(self):
        self.assertEqual(skipped(run(), moved=["rust"]), ["e2e", "image"])

    def test_no_image_is_skipped_when_the_run_kept_none(self):
        self.assertEqual(skipped(run(proof=proof(image="", areas=["image", "rust"]))), ["rust"])


class Record(unittest.TestCase):
    def test_a_record_reads_back_as_a_proof(self):
        text = ci_proof.record(TREE, BASE, ["e2e", "rust", "rust"], IMAGE)
        self.assertEqual(json.loads(text), proof())

    def test_a_malformed_tree_is_refused(self):
        with self.assertRaises(ValueError):
            ci_proof.record("HEAD", BASE, ["rust"], "")


def archive(name, content):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, content)
    return out.getvalue()


class Gather(unittest.TestCase):
    def fetch(self, url):
        answers = {
            f"A/actions/workflows/ci.yaml/runs?event=pull_request&status=success&head_sha={HEAD}&per_page=30":
                {"workflow_runs": [{"id": 7, "head_sha": HEAD, "event": "pull_request", "conclusion": "success",
                                    "path": ".github/workflows/ci.yaml@refs/pull/9/merge"}]},
            "A/actions/runs/7/jobs?per_page=100": {"jobs": [{"conclusion": "success", "labels": ["nzela-pr"]}]},
            "A/actions/runs/7/artifacts?name=ci-proof&per_page=10":
                {"artifacts": [{"name": "ci-proof", "expired": False, "archive_download_url": "A/zip/1"}]},
            "A/zip/1": archive("proof.json", json.dumps(proof())),
        }
        return answers[url]

    def test_the_runs_of_each_head_are_read_through_the_api(self):
        bundle = ci_proof.gather(self.fetch, "A", TREE, [HEAD], LABELS)
        self.assertEqual(believed_bundle(bundle)["run"], 7)

    def test_an_expired_artifact_leaves_the_run_without_a_proof(self):
        def expired(url):
            got = self.fetch(url)
            if "artifacts" in url:
                got["artifacts"][0]["expired"] = True
            return got
        bundle = ci_proof.gather(expired, "A", TREE, [HEAD], LABELS)
        self.assertEqual(believed_bundle(bundle), {})


def believed_bundle(bundle):
    return ci_proof.choose(bundle)


if __name__ == "__main__":
    unittest.main()
