"""Fail-closed judge for durable group sharing and cross-caller readback.

The pinned portal-sharing matrix snapshot ece4c3a2f68557fe8329e9932de36193d0732894
says accepted groups and shares are process-local and lost on restart. That sentence
is the frozen snapshot. Server trunk commit 4bd08872fb47429bd4623e8477c9761b083cf25e
persists accepted groups and item shares in PostgreSQL. This module does not rewrite
the snapshot and does not call a server. The live producer is
certification/portal-group-sharing/produce.py; a document passes only when the caller
binds it to the candidate it names.

Everyone and organization requests stay in notSharedWith and are not grants. Owner,
admin, tenant, scoped-key, and layer policy remain enforced. Absence of retained
observations is nonpass. A fixture-only document cannot qualify.
"""
from __future__ import annotations

import re

SCHEMA = "honua.portal-group-sharing-observations/v1"
SURFACES = ("item", "search", "groupContent", "feature")
PHASES = ("shared", "sharedAfterRestart", "sharedOnReplica", "unshared", "unsharedAfterRestart")
IDENTITY_FIELDS = ("owner", "member", "nonmember", "group", "item")
RUNTIME_FIELDS = ("beforeRestart", "afterRestart", "replica", "store")
POLICY_REFUSALS = ("owner-admin", "tenant", "scoped-key", "layer")
FIELDS = {
    "schema", "candidateSourceSha", "candidateImage", "identities", "runtime",
    "notSharedWith", "publicCallerVisibilityUnchanged", "policyRefusals", "phases",
}
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
IMAGE_RE = re.compile(r"^\S+@sha256:[a-f0-9]{64}$")
MEMBER_SHARED = (True, True, True, True)
HIDDEN = (False, False, False, False)
CORRECTION = (
    "Pinned portal-sharing snapshot ece4c3a2f68557fe8329e9932de36193d0732894 records "
    "process-local groups and shares lost on restart. Server trunk "
    "4bd08872fb47429bd4623e8477c9761b083cf25e persists those accepts in PostgreSQL. "
    "Exact-candidate cross-caller qualification is not claimed."
)


def qualify(document, candidate=None):
    """Judge retained observations.

    Without ``candidate`` a complete document stays nonpass: nothing binds it to the candidate
    being certified. With ``candidate`` ({"sourceSha", "image"} of the minted lock, or of the
    manifest pin for development evidence) a complete document passes only when it names exactly
    that source and image digest; any other binding is refused.
    """
    if not isinstance(document, dict):
        return {"status": "nonpass", "reason": "no-retained-observations"}
    reason = _shape_error(document)
    if reason:
        return {"status": "refused", "reason": reason}
    if candidate is not None:
        if not _bound(document, candidate):
            return {"status": "refused", "reason": "candidate-mismatch"}
        return {
            "status": "pass",
            "reason": "candidate-bound-observations",
            "candidateSourceSha": document["candidateSourceSha"],
            "candidateImage": document["candidateImage"],
        }
    return {
        "status": "nonpass",
        "reason": "exact-candidate-execution-not-claimed",
        "retainedShape": True,
        "correction": CORRECTION,
    }


def _bound(document: dict, candidate) -> bool:
    """The document names the candidate: its source and either the exact image reference or its digest.

    A producer binds the full ``image`` (tag@digest); the protocol-certification ledger knows only the
    candidate ``imageDigest``. A candidate naming neither, or both inconsistently, binds nothing.
    """
    if not isinstance(candidate, dict) or document["candidateSourceSha"] != candidate.get("sourceSha"):
        return False
    image, digest = candidate.get("image"), candidate.get("imageDigest")
    if image is None and digest is None:
        return False
    if image is not None and document["candidateImage"] != image:
        return False
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                               or document["candidateImage"].rsplit("@", 1)[-1] != digest):
        return False
    return True


def _shape_error(document: dict) -> str | None:
    if set(document) != FIELDS or document.get("schema") != SCHEMA:
        return "schema"
    if not isinstance(document.get("candidateSourceSha"), str) or SHA_RE.fullmatch(document["candidateSourceSha"]) is None:
        return "candidate-source"
    if not isinstance(document.get("candidateImage"), str) or IMAGE_RE.fullmatch(document["candidateImage"]) is None:
        return "candidate-image"
    identities = document.get("identities")
    if not isinstance(identities, dict) or set(identities) != set(IDENTITY_FIELDS):
        return "identities"
    if any(not isinstance(identities[name], str) or not identities[name].strip() for name in IDENTITY_FIELDS):
        return "identities"
    if len(set(identities.values())) != len(IDENTITY_FIELDS):
        return "identities"
    runtime = document.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != set(RUNTIME_FIELDS):
        return "runtime"
    if any(not isinstance(runtime[name], str) or not runtime[name].strip() for name in RUNTIME_FIELDS):
        return "runtime"
    if len({runtime["beforeRestart"], runtime["afterRestart"], runtime["replica"]}) != 3:
        return "runtime"
    shared = document.get("notSharedWith")
    if not isinstance(shared, list) or sorted(shared) != ["everyone", "org"] or len(shared) != 2:
        return "not-shared-with"
    if document.get("publicCallerVisibilityUnchanged") is not True:
        return "everyone-or-org-granted"
    refusals = document.get("policyRefusals")
    if not isinstance(refusals, dict) or set(refusals) != set(POLICY_REFUSALS):
        return "policy-refusal"
    if any(refusals[name] != "refused" for name in POLICY_REFUSALS):
        return "policy-refusal"
    return _phase_error(document.get("phases"))


def _flags(view) -> tuple[bool, ...] | None:
    if not isinstance(view, dict) or set(view) != set(SURFACES):
        return None
    if any(type(view[name]) is not bool for name in SURFACES):
        return None
    return tuple(view[name] for name in SURFACES)


def _phase_error(phases) -> str | None:
    if not isinstance(phases, dict) or set(phases) != set(PHASES):
        return "readback"
    observed = {}
    for phase in PHASES:
        callers = phases[phase]
        if not isinstance(callers, dict) or set(callers) != {"member", "nonmember"}:
            return "readback"
        member = _flags(callers["member"])
        nonmember = _flags(callers["nonmember"])
        if member is None or nonmember is None:
            return "readback"
        observed[phase] = (member, nonmember)
    if observed["shared"][0] != MEMBER_SHARED:
        return "member-readback"
    if observed["shared"][1] != HIDDEN:
        return "nonmember-readback"
    if observed["sharedAfterRestart"] != observed["shared"]:
        return "restart-readback"
    if observed["sharedOnReplica"] != observed["shared"]:
        return "replica-readback"
    if observed["unshared"] != (HIDDEN, HIDDEN):
        return "unshare-readback"
    if observed["unsharedAfterRestart"] != (HIDDEN, HIDDEN):
        return "restart-revocation-readback"
    return None
