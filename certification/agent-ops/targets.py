"""Deployment targets. local-docker is the compose project. aws-ecs is unwired on purpose."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from constants import GHCR_SERVER, NEWER_IMAGE, POSTGIS_IMAGE, PRIOR_IMAGE

PRIOR_TAG = PRIOR_IMAGE
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def image_digest(image: str) -> str | None:
    """Repo digest when the image was pulled, otherwise the local image id."""
    completed = _run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}\t{{.Id}}", image])
    if completed.returncode != 0:
        return None
    listed, separator, image_id = completed.stdout.strip().partition("\t")
    if not separator:
        return None
    try:
        digests = json.loads(listed)
    except json.JSONDecodeError:
        digests = []
    if isinstance(digests, list):
        for item in digests:
            if isinstance(item, str) and "@sha256:" in item:
                digest = "sha256:" + item.split("@sha256:", 1)[1]
                if _DIGEST.fullmatch(digest):
                    return digest
    if image_id.startswith("sha256:") and _DIGEST.fullmatch(image_id):
        return image_id
    return None


def _published_newer(newer_sha: str | None, newer_digest: str | None) -> tuple[str | None, str | None]:
    """The deploy workflow publishes native-AOT amd64 as trunk-<sha>-amd64."""
    if newer_digest:
        if not _DIGEST.fullmatch(newer_digest):
            return None, "newer digest is not a sha256 digest"
        return f"{GHCR_SERVER}@{newer_digest}", None
    if newer_sha and _SHA.fullmatch(newer_sha):
        return f"{GHCR_SERVER}:trunk-{newer_sha}-amd64", None
    return None, "a pull needs the recorded trunk sha or a published digest"


def _image_revision(image: str) -> str | None:
    completed = _run([
        "docker", "image", "inspect", "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        image,
    ])
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if not value or value == "<no value>":
        return None
    return value


def _build_newer(source: Path, newer_sha: str) -> subprocess.CompletedProcess[str]:
    """Build the production image from a checkout of the recorded trunk commit."""
    dockerfile = source / "docker" / "Dockerfile.aot"
    argv = [
        "docker", "build",
        "--build-arg", f"HONUA_GIT_SHA={newer_sha}",
        "-f", str(dockerfile),
        "-t", NEWER_IMAGE,
        str(source),
    ]
    if os.environ.get("GITHUB_TOKEN"):
        argv[2:2] = [
            "--secret", "id=github_token,env=GITHUB_TOKEN",
            "--secret", "id=github_actor,env=GITHUB_ACTOR",
        ]
    return _run(argv)


def prepare_images(
    *,
    source: str,
    newer_sha: str | None,
    newer_digest: str | None,
    server_source: Path | None = None,
) -> dict:
    """Pull or build the trunk image, and require the prior image and the database image.

    A published image is ghcr.io/honua-io/honua-server:trunk-<sha>-amd64, or an explicit
    digest. When that tag is missing and a trunk checkout is present, the production
    Dockerfile is built and tagged honua-server:trunk. Local refuses to pull or build.
    """
    note = {
        "ok": False,
        "source": source,
        "prior": {"ref": PRIOR_IMAGE, "digest": None},
        "newer": {"ref": NEWER_IMAGE, "sha": newer_sha, "digest": None},
    }
    if source not in {"local", "pull"}:
        note["detail"] = "image source must be local or pull"
        return note
    if source == "pull":
        remote, error = _published_newer(newer_sha, newer_digest)
        if error or remote is None:
            note["detail"] = error or "a pull needs the recorded trunk sha or a published digest"
            return note
        pulled = _run(["docker", "pull", remote])
        if pulled.returncode == 0:
            tagged = _run(["docker", "tag", remote, NEWER_IMAGE])
            if tagged.returncode != 0:
                note["detail"] = "could not tag the newer image honua-server:trunk"
                return note
        elif server_source is not None and newer_sha and _SHA.fullmatch(newer_sha):
            dockerfile = Path(server_source) / "docker" / "Dockerfile.aot"
            if not dockerfile.is_file():
                note["detail"] = "the trunk checkout has no production dockerfile"
                return note
            built = _build_newer(Path(server_source), newer_sha)
            if built.returncode != 0:
                note["detail"] = f"pull failed for {remote} and the trunk build failed"
                return note
            if _image_revision(NEWER_IMAGE) != newer_sha:
                note["detail"] = "the built image revision does not match the recorded trunk sha"
                return note
        else:
            note["detail"] = f"pull failed for {remote}"
            return note
        for image in (PRIOR_IMAGE, POSTGIS_IMAGE):
            fetched = _run(["docker", "pull", image])
            if fetched.returncode != 0:
                note["detail"] = f"pull failed for {image}"
                return note
    for label, image in (("prior", PRIOR_IMAGE), ("newer", NEWER_IMAGE)):
        digest = image_digest(image)
        if digest is None:
            note["detail"] = f"{image} is not local"
            return note
        note[label]["digest"] = digest
    if image_digest(POSTGIS_IMAGE) is None:
        note["detail"] = f"{POSTGIS_IMAGE} is not local"
        return note
    note["ok"] = True
    return note


def _compose(fixture: Path, state: dict, *args: str) -> subprocess.CompletedProcess[str]:
    project = f"{state['town_slug']}-gis-{state['fixture_token']}"
    env = os.environ.copy()
    env["HONUA_IMAGE"] = state["image_ref"]
    env["HONUA_ADMIN_PASSWORD"] = "local-only"
    env["HONUA_MASTER_KEY"] = "local-only"
    env["POSTGRES_PASSWORD"] = "local-only"
    return _run_env(
        ["docker", "compose", "-p", project, "-f", str(fixture / "compose.yaml"), *args],
        fixture,
        env,
    )


def _run_env(argv: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def start_deployment(fixture: Path, state: dict) -> dict:
    """Create the trial's compose project. Images must already be local."""
    project = f"{state['town_slug']}-gis-{state['fixture_token']}"
    up = _compose(fixture, state, "up", "-d", "--pull", "never")
    note = {
        "executed": up.returncode == 0,
        "server_readback": False,
        "infrastructure": up.returncode != 0,
        "project": project,
    }
    if up.returncode != 0:
        note["detail"] = (up.stderr or up.stdout or "compose up failed").strip()[:300]
    return note


def stop_deployment(fixture: Path, state: dict) -> None:
    _compose(fixture, state, "down", "--remove-orphans", "-t", "1")


def execute_local_docker(fixture: Path, state: dict) -> dict:
    """Start the compose project. Image preparation happens once before any trial."""
    return start_deployment(fixture, state)
