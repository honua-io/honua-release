#!/usr/bin/env python3
"""Mint the entitlement-certification fixture licenses from the server's own test corpus.

honua-server signs its licensing test fixtures with one publicly committed, test-only Ed25519 key
(`tests/dotnet/Honua.TestKit/Helpers/LicenseTestSupport.cs`, key id `test-key`). The candidate
stacks this lane boots trust that key and nothing else, so the fixtures carry no customer, publisher
or production signing material. Nothing minted here is committed: the envelopes are written to a
runtime directory and only their SHA-256 fingerprints reach a receipt.

The fixtures are deterministic (fixed issue date, no expiry, Ed25519 signatures are deterministic),
so the committed trust-anchor fingerprint in fixtures.v1.json binds every run to the same corpus key.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
FIXTURES_PATH = HERE / "fixtures.v1.json"
PROBES_PATH = HERE / "probes.v1.json"
CORPUS_SOURCE = "tests/dotnet/Honua.TestKit/Helpers/LicenseTestSupport.cs"


class FixtureError(ValueError):
    """The corpus key or a fixture specification cannot produce a trusted fixture license."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def fingerprint(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def corpus_signing_seed(source: str) -> bytes:
    """The 32-byte Ed25519 seed declared by LicenseTestSupport.SigningSeed."""
    match = re.search(r"SigningSeed\s*=\s*\[(?P<body>[^\]]*)\]", source)
    if match is None:
        raise FixtureError(f"{CORPUS_SOURCE} no longer declares SigningSeed")
    seed = bytes(int(token, 16) for token in re.findall(r"0x([0-9A-Fa-f]{2})", match.group("body")))
    if len(seed) != 32:
        raise FixtureError(f"{CORPUS_SOURCE} SigningSeed is {len(seed)} bytes, expected 32")
    return seed


def corpus_key_id(source: str) -> str:
    match = re.search(r'const\s+string\s+KeyId\s*=\s*"(?P<id>[^"]+)"', source)
    if match is None:
        raise FixtureError(f"{CORPUS_SOURCE} no longer declares KeyId")
    return match.group("id")


def trusted_public_key(seed: bytes) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "base64url:" + b64url(raw)


def granted_keys(spec: dict, probes: dict) -> list[str]:
    """Entitlement keys a fixture grants: every catalog key, or every key except the gated ones."""
    gated = sorted(probes["gated_keys"])
    community = sorted(probes["community_keys"])
    grant = spec["grants"]
    if grant == "all-catalog-keys":
        return sorted(set(gated) | set(community))
    if grant == "community-keys-only":
        return community
    raise FixtureError(f"fixture {spec['id']} has unknown grant {grant!r}")


def mint(spec: dict, probes: dict, seed: bytes, key_id: str) -> dict:
    payload = json.dumps({
        "schema": "honua.license/v1",
        "licenseId": spec["license_id"],
        "licensedTo": spec["licensed_to"],
        "edition": spec["edition"],
        "issuedAt": spec["issued_at"],
        "entitlements": granted_keys(spec, probes),
    }, separators=(",", ":")).encode("utf-8")
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(payload)
    envelope = json.dumps({"version": 1, "keyId": key_id, "payload": b64url(payload),
                           "signature": b64url(signature)}, separators=(",", ":"))
    return {"id": spec["id"], "edition": spec["edition"], "envelope": envelope,
            "license_fingerprint": fingerprint(envelope), "entitlements": granted_keys(spec, probes)}


def mint_all(source: str, fixtures: dict | None = None, probes: dict | None = None) -> dict:
    fixtures = fixtures if fixtures is not None else json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    probes = probes if probes is not None else json.loads(PROBES_PATH.read_text(encoding="utf-8"))
    seed = corpus_signing_seed(source)
    key_id = corpus_key_id(source)
    anchor = fixtures["trust_anchor"]
    public_key = trusted_public_key(seed)
    if key_id != anchor["key_id"]:
        raise FixtureError(f"corpus key id {key_id!r} does not match the committed {anchor['key_id']!r}")
    if fingerprint(public_key) != anchor["public_key_fingerprint"]:
        raise FixtureError("corpus signing key does not match the committed trust-anchor fingerprint")
    return {"key_id": key_id, "trusted_public_key": public_key,
            "public_key_fingerprint": fingerprint(public_key),
            "fixtures": {spec["id"]: mint(spec, probes, seed, key_id) for spec in fixtures["fixtures"]}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-source", type=Path, required=True,
                        help=f"{CORPUS_SOURCE} from the candidate's exact honua-server commit")
    parser.add_argument("--out", type=Path, required=True,
                        help="runtime directory (never inside the repository) for the minted envelopes")
    args = parser.parse_args()
    try:
        minted = mint_all(args.corpus_source.read_text(encoding="utf-8"))
    except (OSError, FixtureError) as exc:
        print(f"entitlement fixtures: BLOCKED: {exc}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "trusted-key").write_text(minted["trusted_public_key"], encoding="utf-8")
    for fixture_id, fixture in minted["fixtures"].items():
        (args.out / f"{fixture_id}.honua-license.json").write_text(fixture["envelope"], encoding="utf-8")
        print(f"{fixture_id}: {fixture['edition']} {fixture['license_fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
