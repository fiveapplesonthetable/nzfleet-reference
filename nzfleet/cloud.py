"""Read the Cloud Run revision for nzfleet's status screen."""

from . import world
from .config import GCLOUD, GCP_KEY as KEY, GCP_PROJECT as PROJECT, GCP_REGION as REGION


def login() -> None:
    if KEY:
        world.run(GCLOUD, "auth", "activate-service-account", f"--key-file={KEY}", f"--project={PROJECT}")


def live() -> str:
    return world.run(GCLOUD, "run", "services", "describe", "nzela-api", "--region", REGION,
                     "--format=value(metadata.labels.nzela-commit)").strip()
