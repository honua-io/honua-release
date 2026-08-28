#!/usr/bin/env python3
"""Phase 0 source-of-truth gate — validate platform-manifest.yaml + compatibility-matrix.yaml and
enforce compatibility drift.

This is the keystone gate for issue #1: the manifest + matrix are only a credible "source of truth
for iac<->server<->sdk<->db compatibility" if something *checks* them, and that check can FAIL.
Per AGENTS.md: a gate that can't fail is worse than no gate. The tests in tools/test_platform.py
prove each rule below reddens on a real violation.

Three kinds of check:

  STRUCTURE   — both files parse; required keys present; versions/ranges are well-formed; every
                client/component the matrix references exists in the manifest.

  COHERENCE   — the pinned set IS internally consistent: each component's pinned manifest version
                SATISFIES every compatibility-matrix range that names it; sha-pinned couplings
                (iac/helm -> server image, server -> db schema) agree between the two files.
                Bump a pin out of its range, or tighten a range past the pin, and this goes red.

  DRIFT       — compared against a git baseline (the PR base by default), a matrix range may only
                *widen* unless the contract's version is bumped. Narrowing a client's support
                window (raising a floor / lowering a ceiling) without a contract-version bump is a
                breaking change without the required version move -> FAIL. This is the
                manifest/matrix-level half of "a breaking change without a MAJOR bump fails CI";
                the wire-level detectors (buf/OpenAPI/public-API diff) live in the component repos
                (issues #2/#3).

Usage:
  python tools/validate_platform.py                  # structure + coherence (no baseline)
  python tools/validate_platform.py --baseline origin/main   # + drift vs that git ref
  python tools/validate_platform.py --no-drift       # explicitly skip drift even if a baseline exists
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semver  # noqa: E402  (local module, sibling file)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"
MATRIX_PATH = REPO_ROOT / "compatibility-matrix.yaml"

# A component pinned by sha (no release/tag yet) carries this sentinel instead of a semver version.
PRERELEASE_SENTINEL = "pre-release"
SHA_PREFIX = "sha:"

# The only legal non-digest value for honua-server.awsLambdaEcrDigest. ECR re-serialises the OCI
# manifest as Docker schema 2 on push, so that digest cannot be derived from GHCR (nor reproduced by
# a stock `registry:2`, which preserves the source digest) — it is only knowable after a real mirror.
# honua-release#99 tracks replacing it; e2e-cloud-aws.yml rejects it once HONUA_AWS_ROLE_ARN is set.
PENDING_ECR_MIRROR = "pending-ecr-mirror"
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
NPM_INTEGRITY_RE = re.compile(r"sha512-[A-Za-z0-9+/]+={0,2}")
ALLOWED_ECOSYSTEMS = {"npm", "pypi", "nuget"}
ALLOWED_PUBLICATION_STATES = {"published", "promoted", "staged", "unpublished"}
ALLOWED_TRUSTED_EVENTS = {"push", "schedule", "workflow_dispatch", "workflow_run"}


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_yaml_at_ref(ref: str, rel_path: str) -> dict | None:
    """Load a YAML file as it existed at a git ref. Returns None if the ref/path is unavailable
    (e.g. the file is brand new on this branch) — drift is then skipped for it, not failed."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    try:
        return yaml.safe_load(out) or {}
    except yaml.YAMLError:
        return None


# --------------------------------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------------------------------
def _component_version_kind(comp: dict) -> str:
    """'semver' | 'sha' | 'invalid' — how a manifest component declares its version."""
    version = str(comp.get("version", "")).strip()
    if version == PRERELEASE_SENTINEL:
        return "sha" if str(comp.get("sha", "")).strip() else "invalid"
    return "semver" if semver.is_semver(version) else "invalid"


def check_structure(manifest: dict, matrix: dict, f: Findings) -> None:
    for key in ("platformRelease", "status", "components", "protocolCertification"):
        if key not in manifest:
            f.error(f"manifest: missing required top-level key {key!r}")

    certification = manifest.get("protocolCertification") or {}
    cut = str(certification.get("candidateCutAt", "")).strip()
    try:
        parsed_cut = datetime.fromisoformat(cut.replace("Z", "+00:00"))
        if parsed_cut.tzinfo is None:
            raise ValueError
    except ValueError:
        f.error("manifest: protocolCertification.candidateCutAt must be a timezone-aware ISO-8601 timestamp")
    if not _full_sha(certification.get("serverCertificationProducerSha")):
        f.error(
            "manifest: protocolCertification.serverCertificationProducerSha must be a full "
            "40-character commit SHA"
        )
    elif certification.get("serverCertificationProducerSha") != (
        (manifest.get("components") or {}).get("honua-server") or {}
    ).get("sha"):
        f.error(
            "manifest: protocolCertification.serverCertificationProducerSha must match the "
            "frozen honua-server component SHA"
        )

    ledger = certification.get("ledger") or {}
    ledger_status = str(ledger.get("status", "")).strip()
    if ledger_status not in {"pending", "bound"}:
        f.error("manifest: protocolCertification.ledger.status must be 'pending' or 'bound'")
    if ledger.get("repository") != "honua-io/honua-evidence":
        f.error("manifest: protocol certification ledger must be owned by honua-io/honua-evidence")
    ledger_path = str(ledger.get("path", "")).strip()
    if not ledger_path or ledger_path.startswith("/") or ".." in ledger_path.split("/"):
        f.error("manifest: protocol certification ledger path must be a safe repository-relative path")
    if ledger_status == "bound":
        if not re.fullmatch(r"[0-9a-f]{40}", str(ledger.get("commit", "")), re.I):
            f.error("manifest: bound protocol certification ledger commit must be a full SHA")
        if not re.fullmatch(
            r"[0-9a-f]{40}", str(ledger.get("requirementsSourceRevision", "")), re.I
        ):
            f.error(
                "manifest: bound protocol certification ledger requirementsSourceRevision "
                "must be a full SHA"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(ledger.get("sha256", "")), re.I):
            f.error("manifest: bound protocol certification ledger sha256 must be an exact digest")
    elif ledger_status == "pending":
        if (
            ledger.get("commit") != "pending"
            or ledger.get("requirementsSourceRevision") != "pending"
            or ledger.get("sha256") != "pending"
        ):
            f.error(
                "manifest: pending protocol certification ledger must use explicit pending commit, "
                "requirements source, and digest sentinels"
            )
        if manifest.get("status") == "released":
            f.error("manifest: a released platform cannot have a pending protocol certification ledger")

    components = manifest.get("components") or {}
    if not isinstance(components, dict) or not components:
        f.error("manifest: 'components' must be a non-empty mapping")
        return

    for name, comp in components.items():
        comp = comp or {}
        kind = _component_version_kind(comp)
        if kind == "invalid":
            f.error(
                f"manifest: component {name!r} has neither a valid semver 'version' nor a "
                f"'{PRERELEASE_SENTINEL}' + sha pin (version={comp.get('version')!r}, sha={comp.get('sha')!r})"
            )
        if kind == "sha" and not comp.get("sha"):
            f.error(f"manifest: component {name!r} is {PRERELEASE_SENTINEL} but has no sha")

    server = components.get("honua-server") or {}
    for field_name in ("awsEcsArchitecture", "awsLambdaArchitecture"):
        architecture = str(server.get(field_name, "")).strip()
        if architecture not in {"arm64", "x86_64"}:
            f.error(
                f"manifest: honua-server.{field_name} must explicitly select arm64 or x86_64, "
                f"got {architecture!r}"
            )

    # awsLambdaEcrDigest is the digest ECR assigns AFTER the OCI->schema-2 conversion, so it can only
    # be learned by actually pushing to ECR. Exactly two values are legal: a real digest, or the one
    # documented sentinel (honua-release#99). Anything else — a stale digest from a previous pin, an
    # invented one, "TBD" — is rejected here rather than sailing past the cloud gate's regex.
    ecr_digest = str(server.get("awsLambdaEcrDigest", "")).strip()
    if ecr_digest != PENDING_ECR_MIRROR and not re.fullmatch(r"sha256:[0-9a-f]{64}", ecr_digest):
        f.error(
            f"manifest: honua-server.awsLambdaEcrDigest must be an exact sha256:<64hex> digest or "
            f"the literal {PENDING_ECR_MIRROR!r} sentinel, got {ecr_digest!r}"
        )

    # Matrix ranges must parse, and every named client/component must exist in the manifest.
    for contract, body in (matrix.get("contracts") or {}).items():
        for client, spec in (body.get("clients") or {}).items():
            if client not in components:
                f.error(f"matrix: contract {contract!r} names unknown client {client!r} (not in manifest)")
            try:
                semver.parse_range(spec)
            except semver.InvalidRange as e:
                f.error(f"matrix: contract {contract!r} client {client!r} has bad range {spec!r}: {e}")

    for section in ("deploy", "data"):
        for comp_name in (matrix.get(section) or {}):
            if comp_name not in components:
                f.error(f"matrix: {section} names unknown component {comp_name!r} (not in manifest)")

    _check_client_artifacts(manifest.get("clientArtifacts"), f)
    _check_evidence_sources(manifest.get("evidenceSources"), f)


def _mapping(value: object, path: str, f: Findings) -> dict:
    if not isinstance(value, dict) or not value:
        f.error(f"manifest: {path} must be a non-empty mapping")
        return {}
    return value


def _full_sha(value: object) -> bool:
    return bool(FULL_SHA_RE.fullmatch(str(value or "")))


def _check_client_artifacts(value: object, f: Findings) -> None:
    for name, artifact in _mapping(value, "clientArtifacts", f).items():
        path = f"clientArtifacts.{name}"
        if not isinstance(artifact, dict):
            f.error(f"manifest: {path} must be a mapping")
            continue
        ecosystem = artifact.get("ecosystem")
        if ecosystem not in ALLOWED_ECOSYSTEMS:
            f.error(f"manifest: {path}.ecosystem must be one of {sorted(ALLOWED_ECOSYSTEMS)}")
        for key in ("package", "version", "repository"):
            if not str(artifact.get(key, "")).strip():
                f.error(f"manifest: {path}.{key} is required")
        if not semver.is_semver(str(artifact.get("version", ""))):
            f.error(f"manifest: {path}.version must be an exact semver")
        if not _full_sha(artifact.get("sourceSha")):
            f.error(f"manifest: {path}.sourceSha must be a full 40-character commit SHA")
        state = artifact.get("publicationState")
        if state not in ALLOWED_PUBLICATION_STATES:
            f.error(f"manifest: {path}.publicationState must be one of {sorted(ALLOWED_PUBLICATION_STATES)}")
        if not isinstance(artifact.get("targets"), list) or not artifact["targets"]:
            f.error(f"manifest: {path}.targets must be a non-empty list")
        elif any(not isinstance(target, str) or not target.strip() for target in artifact["targets"]):
            f.error(f"manifest: {path}.targets must contain non-empty strings")
        if "required" in artifact and not isinstance(artifact["required"], bool):
            f.error(f"manifest: {path}.required must be a boolean")
        if artifact.get("source") not in {None, "registry", "local", "checkout", "build"}:
            f.error(f"manifest: {path}.source names an unsupported package source")
        digest, integrity = artifact.get("digest"), artifact.get("integrity")
        if digest is not None and not DIGEST_RE.fullmatch(str(digest)):
            f.error(f"manifest: {path}.digest must be sha256:<64hex>")
        if integrity is not None and not NPM_INTEGRITY_RE.fullmatch(str(integrity)):
            f.error(f"manifest: {path}.integrity must be an npm sha512 SRI value")
        if ecosystem == "npm" and digest is not None:
            f.error(f"manifest: {path} must use integrity, not digest, for npm bytes")
        if ecosystem == "npm" and integrity is None:
            f.error(f"manifest: {path} requires npm integrity for immutable package bytes")
        if ecosystem != "npm" and integrity is not None:
            f.error(f"manifest: {path} must use digest, not npm integrity")
        if ecosystem in {"pypi", "nuget"} and digest is None:
            f.error(f"manifest: {path} requires a sha256 digest for immutable package bytes")
        if ecosystem == "pypi" and not str(artifact.get("filename", "")).strip():
            f.error(f"manifest: {path}.filename is required for an exact wheel pin")
        if ecosystem == "nuget" and artifact.get("registry") != "github-packages":
            f.error(f"manifest: {path}.registry must identify the GitHub Packages registry")


def _check_evidence_sources(value: object, f: Findings) -> None:
    for name, source in _mapping(value, "evidenceSources", f).items():
        path = f"evidenceSources.{name}"
        if not isinstance(source, dict):
            f.error(f"manifest: {path} must be a mapping")
            continue
        for key in ("repository", "workflowPath", "trustedBranch", "artifactIdentity", "evidencePolicyRevision"):
            if not str(source.get(key, "")).strip():
                f.error(f"manifest: {path}.{key} is required")
        if not _full_sha(source.get("producerSha")):
            f.error(f"manifest: {path}.producerSha must be a full 40-character commit SHA")
        events = source.get("trustedEvents")
        if not isinstance(events, list) or not events or any(e not in ALLOWED_TRUSTED_EVENTS for e in events):
            f.error(f"manifest: {path}.trustedEvents contains an unsupported or empty event set")
        if "required" in source and not isinstance(source["required"], bool):
            f.error(f"manifest: {path}.required must be a boolean")


def check_legacy_evidence_pin_coherence(manifest: dict, evidence_config: dict | None, f: Findings) -> None:
    """Keep compatibility copies from becoming a second source of pin truth."""
    if evidence_config is None:
        return
    sources = manifest.get("evidenceSources") or {}
    pairs = (("esri-compat", "esri", "evidenceRef"), ("demos", "demos", "sourceRef"))
    for source_name, section, field_name in pairs:
        canonical = str((sources.get(source_name) or {}).get("producerSha", ""))
        legacy = str((evidence_config.get(section) or {}).get(field_name, ""))
        if canonical and legacy and canonical != legacy:
            f.error(
                f"coherence: evidenceSources.{source_name}.producerSha={canonical} disagrees with "
                f"certification/conformance-evidence.yaml {section}.{field_name}={legacy}"
            )


def check_exact_candidate(manifest: dict, f: Findings) -> None:
    """Reject placeholders/fallbacks that cannot certify exact published release bytes."""
    candidate = manifest.get("candidate") or {}
    ref_source = candidate.get("refSource")
    if ref_source != "trunk":
        f.error(
            "exact-candidate: candidate.refSource must be 'trunk'; "
            f"dispatched ref was {ref_source!r}"
        )
    server = ((manifest.get("components") or {}).get("honua-server") or {})
    if not server.get("image") or not DIGEST_RE.fullmatch(str(server.get("digest", ""))):
        f.error("exact-candidate: components.honua-server requires an image and immutable digest")
    # A trunk refSource alone proves nothing about WHICH commit was dispatched: a manifest could
    # claim trunk while pinning (and certifying) a different build. Bind the dispatch ref to the
    # pinned component so the source claim and the certified bytes are about the same commit.
    ref = str(candidate.get("ref", ""))
    server_sha = str(server.get("sha", ""))
    if not _full_sha(ref):
        f.error("exact-candidate: candidate.ref must be a full 40-character commit SHA")
    elif ref != server_sha:
        f.error(
            "exact-candidate: candidate.ref must equal components.honua-server.sha; "
            f"candidate.ref={ref} but the pinned server sha is {server_sha or '<missing>'}"
        )
    for name, artifact in (manifest.get("clientArtifacts") or {}).items():
        path = f"clientArtifacts.{name}"
        if artifact.get("required", True) is False:
            continue
        if artifact.get("publicationState") not in {"published", "promoted"}:
            f.error(f"exact-candidate: {path} does not name published/promoted bytes")
        if not (artifact.get("digest") or artifact.get("integrity")):
            f.error(f"exact-candidate: {path} lacks an immutable digest/integrity pin")
        version = str(artifact.get("version", ""))
        if version in {"", "latest", "next", "local", "pre-release"} or any(c in version for c in "*^~<>"):
            f.error(f"exact-candidate: {path}.version is floating or local")
        source_mode = artifact.get("source")
        if source_mode not in {None, "registry"}:
            f.error(f"exact-candidate: {path} cannot use source={source_mode}; checkout/build fallbacks are forbidden")
    for name, source in (manifest.get("evidenceSources") or {}).items():
        if source.get("required", True) and not _full_sha(source.get("producerSha")):
            f.error(f"exact-candidate: evidenceSources.{name} lacks a trusted immutable producer pin")


# --------------------------------------------------------------------------------------------------
# coherence — the pinned set satisfies the matrix
# --------------------------------------------------------------------------------------------------
def check_coherence(manifest: dict, matrix: dict, f: Findings) -> None:
    components = manifest.get("components") or {}

    # 1. Every pinned semver version satisfies every matrix range that names it.
    for contract, body in (matrix.get("contracts") or {}).items():
        for client, spec in (body.get("clients") or {}).items():
            comp = components.get(client)
            if comp is None:
                continue  # already an error in check_structure
            if _component_version_kind(comp) != "semver":
                # sha-pinned (un-released) client: the matrix floor is its declared (un-shipped)
                # version; there is no shippable semver to satisfy yet. Skip — not a violation.
                continue
            version = str(comp["version"]).strip()
            try:
                if not semver.satisfies(version, spec):
                    f.error(
                        f"coherence: {client} is pinned at {version} in the manifest but does NOT "
                        f"satisfy the {contract!r} contract range {spec!r} in the matrix"
                    )
            except (semver.InvalidRange, semver.InvalidVersion) as e:
                f.error(f"coherence: cannot test {client} {version!r} against {spec!r}: {e}")

    # 2. sha-pinned deploy/data couplings agree across the two files.
    server = components.get("honua-server") or {}
    server_sha = str(server.get("sha", "")).strip()
    for comp_name, body in (matrix.get("deploy") or {}).items():
        for field_name in ("deploysServerImage", "appVersion"):
            val = str((body or {}).get(field_name, "")).strip()
            if val.startswith(SHA_PREFIX):
                pinned = val[len(SHA_PREFIX):]
                if server_sha and pinned != server_sha:
                    f.error(
                        f"coherence: matrix deploy.{comp_name}.{field_name} pins server "
                        f"sha {pinned} but the manifest pins honua-server at {server_sha}"
                    )

    matrix_db = str(((matrix.get("data") or {}).get("honua-server") or {}).get("requiresDbSchema", "")).strip()
    manifest_db = str(server.get("dbSchema", "")).strip()
    if matrix_db and manifest_db and not matrix_db.startswith(">") and matrix_db != manifest_db:
        f.error(
            f"coherence: matrix data.honua-server.requiresDbSchema={matrix_db!r} disagrees with "
            f"manifest honua-server.dbSchema={manifest_db!r}"
        )


# --------------------------------------------------------------------------------------------------
# drift — a range may only widen unless the contract version bumped
# --------------------------------------------------------------------------------------------------
def _range_or_none(spec: str) -> semver.Range | None:
    try:
        return semver.parse_range(spec)
    except semver.InvalidRange:
        return None


def check_drift(matrix: dict, baseline: dict, f: Findings) -> None:
    base_contracts = baseline.get("contracts") or {}
    cur_contracts = matrix.get("contracts") or {}

    for contract, cur_body in cur_contracts.items():
        base_body = base_contracts.get(contract)
        if base_body is None:
            continue  # new contract surface — nothing to compare
        contract_bumped = str(cur_body.get("version", "")) != str(base_body.get("version", ""))

        cur_clients = cur_body.get("clients") or {}
        base_clients = base_body.get("clients") or {}

        for client, base_spec in base_clients.items():
            if client not in cur_clients:
                if not contract_bumped:
                    f.error(
                        f"drift: client {client!r} was dropped from contract {contract!r} without a "
                        f"contract-version bump (dropping a supported client is breaking)"
                    )
                continue
            base_range = _range_or_none(base_spec)
            cur_range = _range_or_none(cur_clients[client])
            if not base_range or not cur_range:
                continue
            narrowed = _narrowed(base_range, cur_range)
            if narrowed and not contract_bumped:
                f.error(
                    f"drift: contract {contract!r} client {client!r} narrowed its support window "
                    f"({base_range.raw!r} -> {cur_range.raw!r}: {narrowed}) without a contract-version "
                    f"bump — breaking change without the required version move"
                )


def _narrowed(base: semver.Range, cur: semver.Range) -> str:
    """Return a human reason if `cur` drops support that `base` granted, else ''."""
    reasons = []
    # Floor raised: versions at/above the old floor but below the new floor lost support.
    if base.floor is not None and cur.floor is not None and cur.floor > base.floor:
        reasons.append(f"floor raised {base.floor} -> {cur.floor}")
    if base.floor is None and cur.floor is not None:
        reasons.append(f"added a floor {cur.floor} (was open-below)")
    # Ceiling lowered: versions below the old ceiling but at/above the new one lost support.
    if base.ceiling is not None and cur.ceiling is not None and cur.ceiling < base.ceiling:
        reasons.append(f"ceiling lowered {base.ceiling} -> {cur.ceiling}")
    if base.ceiling is None and cur.ceiling is not None:
        reasons.append(f"added a ceiling {cur.ceiling} (was open-above)")
    return "; ".join(reasons)


# --------------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------------
def validate(manifest: dict, matrix: dict, baseline_matrix: dict | None, exact_candidate: bool = False) -> Findings:
    f = Findings()
    check_structure(manifest, matrix, f)
    # Coherence/drift assume structure held well enough to read; they no-op on missing pieces.
    check_coherence(manifest, matrix, f)
    if baseline_matrix is not None:
        check_drift(matrix, baseline_matrix, f)
    if exact_candidate:
        check_exact_candidate(manifest, f)
    return f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default=None,
                    help="git ref to diff against for drift enforcement (e.g. origin/main)")
    ap.add_argument("--no-drift", action="store_true", help="skip drift even if --baseline is given")
    ap.add_argument("--manifest", default=str(MANIFEST_PATH))
    ap.add_argument("--matrix", default=str(MATRIX_PATH))
    ap.add_argument("--exact-candidate", action="store_true",
                    help="reject unpublished/floating/local pins; use for release certification")
    args = ap.parse_args(argv)

    manifest = _load_yaml(Path(args.manifest))
    matrix = _load_yaml(Path(args.matrix))

    baseline_matrix: dict | None = None
    if args.baseline and not args.no_drift:
        baseline_matrix = _load_yaml_at_ref(args.baseline, "compatibility-matrix.yaml")
        if baseline_matrix is None:
            print(f"note: no baseline compatibility-matrix.yaml at {args.baseline!r}; skipping drift")

    f = validate(manifest, matrix, baseline_matrix, exact_candidate=args.exact_candidate)
    evidence_path = REPO_ROOT / "certification" / "conformance-evidence.yaml"
    if Path(args.manifest).resolve() == MANIFEST_PATH.resolve() and evidence_path.exists():
        check_legacy_evidence_pin_coherence(manifest, _load_yaml(evidence_path), f)

    for w in f.warnings:
        print(f"WARN  {w}")
    for e in f.errors:
        print(f"ERROR {e}")

    if f.ok:
        scope = "structure + coherence" + (" + drift" if baseline_matrix is not None else "")
        print(f"OK    platform manifest + compatibility matrix valid ({scope})")
        return 0
    print(f"\nFAILED: {len(f.errors)} error(s), {len(f.warnings)} warning(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
