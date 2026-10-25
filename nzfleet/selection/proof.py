"""What a pull request run of ci.yaml proved about a tree, and whether main may lean on it.

Standard library only, because ci runs this from a checkout.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

WORKFLOW = ".github/workflows/ci.yaml"
ARTIFACT = "ci-proof"
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
AREA = re.compile(r"[a-z0-9-]+")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def record(tree: str, base: str, areas: list, image_id: str) -> str:
    """The proof document for a run that tested `tree`, whose changes were measured from `base`."""
    document = {"tree": tree, "base": base, "areas": sorted(set(areas)), "image": image_id}
    if not well_formed(document):
        raise ValueError(f"not a proof: {document}")
    return json.dumps(document, sort_keys=True)


def well_formed(document) -> bool:
    return (
        isinstance(document, dict)
        and isinstance(document.get("tree"), str) and bool(COMMIT.fullmatch(document["tree"]))
        and isinstance(document.get("base"), str) and bool(COMMIT.fullmatch(document["base"]))
        and isinstance(document.get("areas"), list)
        and all(isinstance(a, str) and AREA.fullmatch(a) for a in document["areas"])
        and isinstance(document.get("image"), str) and (document["image"] == "" or bool(IMAGE_ID.fullmatch(document["image"])))
    )


def _ran_on_our_machines(jobs, labels) -> bool:
    ran = [job for job in jobs if job.get("conclusion") != "skipped"]
    return bool(ran) and all(set(job.get("labels") or []) & set(labels) for job in ran)


def _believable(run: dict, bundle: dict) -> bool:
    return (
        run.get("event") == "pull_request"
        and run.get("conclusion") == "success"
        and str(run.get("path", "")).split("@")[0] == WORKFLOW
        and run.get("head_sha") in bundle["heads"]
        and _ran_on_our_machines(run.get("jobs") or [], bundle["labels"])
        and well_formed(run.get("proof"))
        and run["proof"]["tree"] == bundle["tree"]
    )


def choose(bundle: dict) -> dict:
    """The newest run of this repository's ci.yaml main may lean on, or {} when none qualifies.

    A run qualifies when it was a pull request run that succeeded entirely on a machine carrying one of
    `labels`, its head is the commit main is at or a parent of it, and the proof it uploaded names exactly
    `tree`. The proof is the run's own statement; the head and the machines are GitHub's.
    """
    found = [run for run in bundle["runs"] if _believable(run, bundle)]
    if not found:
        return {}
    best = max(found, key=lambda run: run["id"])
    return {"run": best["id"], "base": best["proof"]["base"], "areas": best["proof"]["areas"], "image": best["proof"]["image"]}


def proved(claimed: list, moved: list, image_id: str) -> list:
    """The areas main skips: those the run passed that nothing has changed under since its branch point.

    `image` counts only when the run kept the image it built, since deploy takes that image or builds none.
    """
    skipped = [area for area in claimed if area not in moved and area != "image"]
    return sorted(set(skipped + (["image"] if image_id else [])))


def gather(fetch, api: str, tree: str, heads: list, labels: list) -> dict:
    """The pull request runs of each head, with their jobs and uploaded proof, as `choose` reads them."""
    runs = []
    for head in heads:
        listing = fetch(f"{api}/actions/workflows/ci.yaml/runs?event=pull_request&status=success&head_sha={head}&per_page=30")
        for found in listing.get("workflow_runs", []):
            run = {key: found.get(key) for key in ("id", "head_sha", "event", "conclusion", "path")}
            run["jobs"] = fetch(f"{api}/actions/runs/{found['id']}/jobs?per_page=100").get("jobs", [])
            run["proof"] = _uploaded_proof(fetch, api, found["id"])
            runs.append(run)
    return {"tree": tree, "heads": heads, "labels": labels, "runs": runs}


def _uploaded_proof(fetch, api: str, run_id: int):
    listing = fetch(f"{api}/actions/runs/{run_id}/artifacts?name={ARTIFACT}&per_page=10")
    for artifact in listing.get("artifacts", []):
        if artifact.get("name") != ARTIFACT or artifact.get("expired"):
            continue
        with zipfile.ZipFile(io.BytesIO(fetch(artifact["archive_download_url"]))) as archive:
            return json.loads(archive.read("proof.json"))
    return None


class _KeepTokenHome(urllib.request.HTTPRedirectHandler):
    """An artifact download redirects to blob storage, which must not be sent the repository token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def _github(token: str):
    opener = urllib.request.build_opener(_KeepTokenHome)

    def fetch(url: str):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with opener.open(request) as response:
            body = response.read()
            return json.loads(body) if "json" in response.headers.get("Content-Type", "") else body

    return fetch


def select(api: str, tree: str, labels: str, heads: list) -> int:
    try:
        bundle = gather(_github(os.environ.get("GH_TOKEN", "")), api, tree, heads, [x for x in labels.split(",") if x])
        chosen = choose(bundle)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"no proof read: {error}", file=sys.stderr)
        chosen = {}
    if chosen:
        print(f"run={chosen['run']}\nbase={chosen['base']}\nareas={' '.join(chosen['areas'])}\nimage={chosen['image']}")
    return 0
