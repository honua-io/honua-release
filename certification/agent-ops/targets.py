"""Deployment targets. local-docker is the compose project. aws-ecs is unwired on purpose."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fixture import compose_config

PRIOR_TAG = "ghcr.io/honua-io/honua-server:2026.1-rc"


def local_docker_plan(fixture: Path, state: dict) -> dict:
    compose = (fixture / "compose.yaml").read_text(encoding="utf-8")
    if "${HONUA_IMAGE}" not in compose:
        raise RuntimeError("compose file does not pin the server image from HONUA_IMAGE")
    return {
        "target": "local-docker",
        "compose": str(fixture / "compose.yaml"),
        "prior_image": PRIOR_TAG,
        "running_image": state["image_ref"],
        "newer_image": state["target_image"],
        "execute": False,
    }


def aws_ecs_plan() -> dict:
    """Cell wiring belongs to the release session. This lane does not call it."""
    return {
        "target": "aws-ecs",
        "status": "unwired",
        "redis": "on",
        "detail": (
            "aws-ecs stays unwired in this lane. The release session connects it to the "
            "cloud cell runner later. No cell is started here."
        ),
    }


def execute_local_docker(fixture: Path, state: dict) -> dict:
    """Render compose and inspect images that are already local. Does not pull or read layers back."""
    note = {"executed": False, "server_readback": False, "infrastructure": False}
    try:
        rendered = compose_config(fixture)
    except (OSError, RuntimeError) as exc:
        note["infrastructure"] = True
        note["detail"] = str(exc)[:300]
        return note
    note["rendered_names_image"] = state["image_ref"] in rendered or PRIOR_TAG in rendered
    for label, image in (("prior", PRIOR_TAG), ("newer", state["target_image"])):
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            check=False,
        )
        note[f"{label}_id"] = completed.stdout.strip() if completed.returncode == 0 else None
    note["executed"] = True
    return note
