#!/usr/bin/env python3
"""Bind a verified server image attestation to the candidate manifest's source commit.

`gh attestation verify oci://<image>@<digest>` proves the image was produced by the pinned
nightly workflow on trunk, but a trunk-only policy accepts ANY trunk build. The gp-outputs job
builds the worker from `components.honua-server.sha` and qualifies it against the server at
`components.honua-server.digest`; when those two fields name different trunk builds the pairing
is not the candidate, and a receipt produced from it must not exist. This check refuses the
verification output unless every verified statement's subject is the manifest digest and the
attested source commit (certificate and SLSA resolved dependency) is the manifest sha.

Usage: python3 tools/check_image_attestation.py --manifest platform-manifest.yaml \
           --attestation image-attestation.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


class AttestationMismatch(Exception):
    pass


def manifest_pin(path: Path) -> tuple[str, str]:
    server = yaml.safe_load(path.read_text(encoding="utf-8"))["components"]["honua-server"]
    sha, digest = str(server.get("sha", "")), str(server.get("digest", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise AttestationMismatch(f"manifest honua-server sha {sha!r} is not a full commit SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise AttestationMismatch(f"manifest honua-server digest {digest!r} is not an immutable sha256 digest")
    return sha, digest


def check(verified, sha: str, digest: str) -> int:
    """Return the number of verified attestations, all bound to (sha, digest); raise otherwise."""
    if not isinstance(verified, list) or not verified:
        raise AttestationMismatch("no verified attestation was returned for the candidate server image")
    algorithm, _, value = digest.partition(":")
    for index, entry in enumerate(verified):
        result = (entry or {}).get("verificationResult") or {}
        statement = result.get("statement") or {}
        subjects = [(subject.get("digest") or {}).get(algorithm) for subject in statement.get("subject") or []]
        if value not in subjects:
            raise AttestationMismatch(
                f"attestation {index} subject(s) {subjects} do not include the manifest image digest {digest}")
        certificate = (result.get("signature") or {}).get("certificate") or {}
        source = certificate.get("sourceRepositoryDigest")
        if source != sha:
            raise AttestationMismatch(
                f"attestation {index} was built from source {source}, not the manifest server sha {sha}; "
                "the manifest digest and sha name different builds")
        build = (statement.get("predicate") or {}).get("buildDefinition") or {}
        commits = {(dependency.get("digest") or {}).get("gitCommit")
                   for dependency in build.get("resolvedDependencies") or []} - {None}
        if commits != {sha}:
            raise AttestationMismatch(
                f"attestation {index} resolves source commit(s) {sorted(commits)}, not the manifest server sha {sha}")
    return len(verified)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--attestation", required=True, type=Path,
                        help="`gh attestation verify --format json` output for the manifest image")
    args = parser.parse_args(argv)
    try:
        sha, digest = manifest_pin(args.manifest)
        count = check(json.loads(args.attestation.read_text(encoding="utf-8")), sha, digest)
    except (AttestationMismatch, OSError, ValueError, KeyError, TypeError) as error:
        print(f"::error::server image attestation is not bound to the candidate: {error}", file=sys.stderr)
        return 1
    print(f"server image {digest} attested from source {sha} ({count} verified attestation(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
