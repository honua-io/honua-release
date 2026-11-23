"""Compare the GA server's installed identity, runtime version and OCI version.

Evidence comes from the actual Compose candidate container and its pulled image.
The install/rollback phases cannot substitute for a missing candidate observation.
RC/nightly runs retain their existing policy until they carry a versioned pair.

The server image stamps its informational version as `<version>+<sha7>` (honua-server Dockerfile,
scripts/ci/container-version.py), so the runtime answer may carry exactly that build suffix. The
suffix must name the candidate's source revision; any other build metadata is refused.
"""
from __future__ import annotations

from platform_version import artifact_version
from upgrade_trunk_pair import DIGEST

BUILD_SUFFIX_LENGTH = 7  # the Dockerfile stamps `+%.7s` of the source revision
REVISION_LENGTH = 40


def runtime_platform_version(observed, source_revision) -> str | None:
    """The platform version a runtime answer reports, or None when its build suffix is foreign."""
    if not isinstance(observed, str):
        return None
    version, plus, build = observed.partition("+")
    if not plus:
        return version
    revision = str(source_revision or "")
    if len(build) == BUILD_SUFFIX_LENGTH and len(revision) == REVISION_LENGTH and revision.startswith(build):
        return version
    return None


def version_evidence(pair: dict, phases: dict) -> dict:
    """Return a receipt fragment; missing or inconsistent GA evidence fails closed."""
    expected = pair.get("candidate_version")
    if expected in (None, ""):
        return {"status": "not-applicable", "why": "pair has no platform version requirement"}
    result = {"status": "fail", "expectedVersion": expected}
    try:
        if not isinstance(expected, str) or artifact_version(expected) != expected:
            raise ValueError("expected version must be a normalized artifact version")
    except ValueError:
        return {**result, "why": "expected version must be a normalized artifact version"}
    phase = phases.get("candidate")
    if not isinstance(phase, dict):
        return {**result, "why": "candidate phase identity is missing"}
    runtime = phase.get("runtimeIdentity")
    if not isinstance(runtime, dict):
        return {**result, "why": "candidate runtime identity is missing"}
    reference = pair.get("candidate")
    if (not isinstance(reference, str) or "@" not in reference
            or not DIGEST.fullmatch(reference.rsplit("@", 1)[1])):
        return {**result, "why": "candidate pair has no immutable image reference"}
    if (runtime.get("imageReference") != reference
            or runtime.get("imageDigest") != reference.rsplit("@", 1)[1]
            or not phase.get("imageID") or phase.get("imageID") != phase.get("expectedImageID")
            or phase.get("platform") != "linux/amd64"):
        return {**result, "why": "observed container is not the pair's amd64 candidate image"}
    result.update(runtimeVersion=runtime.get("observedVersion"), imageVersion=phase.get("imageVersion"))
    runtime_version = runtime_platform_version(result["runtimeVersion"], pair.get("candidate_source_revision"))
    if runtime_version != expected or result["imageVersion"] != expected:
        return {**result, "why": "runtime and OCI versions must both equal the candidate platform version"}
    return {**result, "status": "pass", "why": "installed runtime and OCI versions match the candidate"}
