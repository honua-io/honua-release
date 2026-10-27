"""The expected-GA capability observation: what a boot of the candidate server actually advertised.

Two files, two owners (honua-release#183, nightly expected-GA binding):

  e2e/expected-ga-manifest.json     POLICY, reviewed in git: which capability ids must be GA
                                    (`expectedGa`), which are asserted supported-only
                                    (`availabilityGated`) and which are not asserted (`excluded`).
  e2e/expected-ga-observation.json  OBSERVATION, generated per candidate by
                                    tools/expected_ga_snapshot.py from a boot of the candidate's own
                                    honua-server image (e2e/harness/boot.sh). It records the server
                                    source sha, the image index digest and the booted child digest.

Every consumer (tools/validate_platform.py, e2e/canonical_checks.py) calls `binding_errors` against
the candidate's `components.honua-server` and refuses on any mismatch: an observation of another
server, or one with no binding at all, is never a pass and never a skip.

Stdlib only: the cloud cells import this from e2e/ without the release tool dependencies.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

E2E_DIR = Path(__file__).resolve().parent
POLICY_PATH = E2E_DIR / "expected-ga-manifest.json"
OBSERVATION_PATH = E2E_DIR / "expected-ga-observation.json"
OBSERVATION_SCHEMA = "honua.expected-ga-observation.v1"
CAPABILITY_MANIFEST_SCHEMA = "honua.capability_manifest.v1"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
REFRESH = "regenerate it with tools/expected_ga_snapshot.py from a boot of the candidate server image"


def load_json(path: str | Path) -> dict | None:
    """Return the JSON object at `path`, or None when it is missing, unreadable or not an object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ids(entries: Any) -> set[str]:
    return {e.get("id") for e in (entries or []) if isinstance(e, dict) and isinstance(e.get("id"), str)}


def checked_ids(policy: dict) -> list[str]:
    """Policy ids asserted supported=true: `expectedGa` minus `excluded`."""
    excluded = _ids(policy.get("excluded"))
    return [i for i in (policy.get("expectedGa") or []) if isinstance(i, str) and i not in excluded]


def _capabilities(manifest: Any, label: str) -> dict[str, dict]:
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != CAPABILITY_MANIFEST_SCHEMA:
        raise ValueError(f"{label} capability manifest is not {CAPABILITY_MANIFEST_SCHEMA}")
    caps = manifest.get("capabilities")
    if not isinstance(caps, list):
        raise ValueError(f"{label} capability manifest has no capabilities[] array")
    by_id: dict[str, dict] = {}
    for cap in caps:
        if not isinstance(cap, dict) or not isinstance(cap.get("id"), str) or not cap["id"]:
            raise ValueError(f"{label} capability manifest has an entry without an id")
        if cap["id"] in by_id:
            raise ValueError(f"{label} capability manifest repeats {cap['id']}")
        by_id[cap["id"]] = cap
    return by_id


def build_observation(*, server_sha: str, image: str, index_digest: str, booted_platform: str,
                      booted_child_digest: str, image_revision_label: str | None,
                      anonymous: dict, authenticated: dict, fetched_at: str, boot: dict) -> dict:
    """Bind one boot's two capability-manifest reads to the candidate server, or refuse.

    Refuses when the booted server does not advertise the candidate's own commit (commit-sha
    sourced), when the image's revision label names another commit, or when the anonymous and
    authenticated reads disagree on any identity, lifecycle or supported flag.
    """
    if not SHA_RE.fullmatch(server_sha or ""):
        raise ValueError(f"candidate server sha {server_sha!r} is not a full commit sha")
    for name, digest in (("index", index_digest), ("booted child", booted_child_digest)):
        if not DIGEST_RE.fullmatch(digest or ""):
            raise ValueError(f"{name} digest {digest!r} is not an exact sha256 digest")
    if image_revision_label != server_sha:
        raise ValueError(f"booted image labels revision {image_revision_label!r}, not the candidate {server_sha}")
    server = anonymous.get("server") if isinstance(anonymous, dict) else None
    server = server if isinstance(server, dict) else {}
    revision, source = server.get("deploymentRevision"), server.get("deploymentRevisionSource")
    if revision != server_sha or source != "commit-sha":
        raise ValueError(f"booted server advertises deploymentRevision={revision!r} "
                         f"(source {source!r}), not the candidate {server_sha} (commit-sha)")
    anon, auth = _capabilities(anonymous, "anonymous"), _capabilities(authenticated, "authenticated")
    if set(anon) != set(auth):
        raise ValueError(f"anonymous and authenticated reads advertise different ids: "
                         f"{sorted(set(anon) ^ set(auth))}")
    capabilities = []
    for cap_id in sorted(anon):
        a, b = anon[cap_id], auth[cap_id]
        for field in ("lifecycle", "supported"):
            if a.get(field) != b.get(field):
                raise ValueError(f"{cap_id}: anonymous {field}={a.get(field)!r} but authenticated {b.get(field)!r}")
        capabilities.append({"id": cap_id, "lifecycle": a.get("lifecycle"), "supported": a.get("supported"),
                             "available": a.get("available"), "availableAuthenticated": b.get("available")})
    return {
        "schemaVersion": OBSERVATION_SCHEMA,
        "_comment": "GENERATED by tools/expected_ga_snapshot.py; do not edit. Observation of one boot of "
                    "the bound honua-server image. Policy lives in e2e/expected-ga-manifest.json.",
        "boundCandidate": {"serverSha": server_sha, "image": image, "imageIndexDigest": index_digest,
                           "bootedPlatform": booted_platform, "bootedChildDigest": booted_child_digest},
        "deploymentRevision": revision,
        "deploymentRevisionSource": source,
        "manifestSchemaVersion": CAPABILITY_MANIFEST_SCHEMA,
        "fetchedAt": fetched_at,
        "boot": boot,
        "totalCapabilities": len(capabilities),
        "capabilities": capabilities,
    }


def binding_errors(observation: Any, server: Any) -> list[str]:
    """Why `observation` is not bound to the candidate `components.honua-server`; [] when it is."""
    where = OBSERVATION_PATH.name
    if not isinstance(observation, dict):
        return [f"{where} is missing or unreadable; the expected-GA snapshot is unbound ({REFRESH})"]
    if observation.get("schemaVersion") != OBSERVATION_SCHEMA:
        return [f"{where}: schemaVersion must be {OBSERVATION_SCHEMA!r} ({REFRESH})"]
    server = server if isinstance(server, dict) else {}
    bound = observation.get("boundCandidate")
    bound = bound if isinstance(bound, dict) else {}
    want_sha, want_digest = server.get("sha"), server.get("digest")
    errors = []
    if not isinstance(want_sha, str) or not SHA_RE.fullmatch(want_sha):
        errors.append(f"candidate components.honua-server.sha {want_sha!r} is not a full commit sha")
    if not isinstance(want_digest, str) or not DIGEST_RE.fullmatch(want_digest):
        errors.append(f"candidate components.honua-server.digest {want_digest!r} is not an exact image digest")
    if errors:
        return errors
    if bound.get("serverSha") != want_sha:
        errors.append(f"{where}: boundCandidate.serverSha={bound.get('serverSha')!r} is not the candidate "
                      f"components.honua-server.sha={want_sha!r}; the snapshot belongs to another server ({REFRESH})")
    if bound.get("imageIndexDigest") != want_digest:
        errors.append(f"{where}: boundCandidate.imageIndexDigest={bound.get('imageIndexDigest')!r} is not the "
                      f"candidate components.honua-server.digest={want_digest!r} ({REFRESH})")
    platform = bound.get("bootedPlatform")
    children = server.get("platformDigests") if isinstance(server.get("platformDigests"), dict) else {}
    if not isinstance(platform, str) or bound.get("bootedChildDigest") != children.get(platform):
        errors.append(f"{where}: boundCandidate.bootedChildDigest={bound.get('bootedChildDigest')!r} is not the "
                      f"candidate platformDigests.{platform}={children.get(platform)!r} ({REFRESH})")
    if observation.get("deploymentRevision") != want_sha or observation.get("deploymentRevisionSource") != "commit-sha":
        errors.append(f"{where}: the booted server advertised deploymentRevision="
                      f"{observation.get('deploymentRevision')!r} (source {observation.get('deploymentRevisionSource')!r}), "
                      f"not the candidate {want_sha!r} (commit-sha)")
    return errors


def policy_errors(policy: Any, observation: dict) -> list[str]:
    """Policy ids the bound observation does not advertise supported=true; [] when all are."""
    if not isinstance(policy, dict) or not isinstance(policy.get("expectedGa"), list):
        return [f"{POLICY_PATH.name} is missing or malformed"]
    observed = {c.get("id"): c for c in observation.get("capabilities") or [] if isinstance(c, dict)}
    missing = sorted(i for i in checked_ids(policy) if observed.get(i, {}).get("supported") is not True)
    if missing:
        return [f"{OBSERVATION_PATH.name}: the candidate boot does not advertise expected-GA ids "
                f"supported=true: {missing}"]
    return []
