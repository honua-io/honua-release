#!/usr/bin/env python3
"""Generate e2e/expected-ga-observation.json from a boot of the candidate's own honua-server.

The nightly certifies whichever server `tools/resolve_trunk_candidate.py` resolves, so the
expected-GA observation cannot be a hand-pinned file: it is generated here for exactly that server.

  1. Read components.honua-server (sha, image, index digest, platformDigests) from --manifest.
  2. Re-read the immutable registry index and take the child for the docker host's architecture;
     it must equal the manifest's platformDigests entry.
  3. Boot that index digest with the candidate harness (e2e/harness/boot.sh, compose.candidate.yml).
  4. Read /api/v1/capabilities/manifest anonymously and with the admin X-API-Key, and refuse unless
     the server advertises the candidate commit (commit-sha) and both reads agree.
  5. Write the bound observation, then check it against the reviewed policy
     (e2e/expected-ga-manifest.json): every expected-GA id must be advertised supported=true.

Any refusal exits 1 and writes nothing; the bind step then has no observation and refuses too.

  python tools/expected_ga_snapshot.py --manifest resolved-candidate/platform-manifest.yaml \
      --out e2e/expected-ga-observation.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.append(str(ROOT / "e2e"))
import expected_ga_observation as ego  # noqa: E402
from image_platforms import registry_image_platform_digests  # noqa: E402

BOOT = ROOT / "e2e" / "harness" / "boot.sh"
DEFAULT_KEY = "honua-console-dev-key"   # compose.candidate.yml HONUA_ADMIN_PASSWORD default


def candidate_server(manifest_path: Path) -> dict:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    server = (manifest.get("components") or {}).get("honua-server")
    if not isinstance(server, dict):
        raise ValueError(f"{manifest_path}: no components.honua-server")
    return server


def coordinate(image: str) -> str:
    """Registry coordinate of a tag- or digest-named image (no tag, no digest)."""
    name = str(image).split("@", 1)[0]
    head, _, tail = name.rpartition("/")
    return f"{head}/{tail.split(':', 1)[0]}" if head else tail.split(":", 1)[0]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def fetch_json(url: str, key: str | None = None) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **({"X-API-Key": key} if key else {})})
    last = None
    for delay in (0, 2, 5, 10):
        time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
    raise ValueError(f"GET {url} failed: {last}")


def generate(server: dict, *, base_url: str, api_key: str, inspector=registry_image_platform_digests,
             boot=None, fetch=fetch_json, host_arch=None, image_label=None, now=None) -> dict:
    """Boot the candidate server and return its bound observation. Injectable for tests."""
    sha, digest = server.get("sha"), server.get("digest")
    image = f"{coordinate(server.get('image') or '')}@{digest}"
    arch = host_arch() if host_arch else docker("version", "-f", "{{.Server.Arch}}")
    children = inspector(coordinate(server.get("image") or ""), digest)
    declared = server.get("platformDigests") or {}
    child = children.get(arch)
    if not child or child != declared.get(arch):
        raise ValueError(f"registry index {digest} linux/{arch} child {child!r} is not the candidate "
                         f"platformDigests.{arch}={declared.get(arch)!r}")
    (boot or _boot)(image)
    label = image_label(image) if image_label else docker(
        "image", "inspect", image, "-f", '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    url = base_url.rstrip("/") + "/api/v1/capabilities/manifest"
    return ego.build_observation(
        server_sha=sha, image=image, index_digest=digest, booted_platform=arch, booted_child_digest=child,
        image_revision_label=label, anonymous=fetch(url), authenticated=fetch(url, api_key),
        fetched_at=(now or (lambda: datetime.now(timezone.utc).isoformat()))(),
        boot={"harness": "e2e/harness/boot.sh (compose.candidate.yml: PostGIS 16-3.4, durable Redis, "
                         "Licensing__Mode=Disabled)", "url": url})


def _boot(image: str) -> None:
    env = {**os.environ, "HONUA_SERVER_IMAGE": image}
    subprocess.run(["bash", str(BOOT), "up"], check=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    ap.add_argument("--policy", type=Path, default=ego.POLICY_PATH)
    ap.add_argument("--out", type=Path, default=ego.OBSERVATION_PATH)
    ap.add_argument("--keep-up", action="store_true", help="leave the booted stack running")
    args = ap.parse_args(argv)
    base = os.environ.get("E2E_BASE") or f"http://localhost:{os.environ.get('E2E_SERVER_PORT', '8080')}"
    try:
        server = candidate_server(args.manifest)
        try:
            observation = generate(server, base_url=base, api_key=os.environ.get("E2E_API_KEY") or DEFAULT_KEY)
        finally:
            if not args.keep_up:
                subprocess.run(["bash", str(BOOT), "down"], check=False)
        errors = ego.binding_errors(observation, server) + ego.policy_errors(ego.load_json(args.policy), observation)
        if errors:
            raise ValueError("; ".join(errors))
    except (OSError, ValueError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        print(f"ERROR expected-GA observation refused: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    bound = observation["boundCandidate"]
    print(f"OK    {args.out}: {observation['totalCapabilities']} capabilities from {bound['image']} "
          f"(linux/{bound['bootedPlatform']} {bound['bootedChildDigest']}) at {bound['serverSha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
