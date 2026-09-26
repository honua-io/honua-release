#!/usr/bin/env python3
"""Publication gate for customer-install-manifest.json (honua-server#4300, release#314).

The customer installation profile is copied byte-for-byte to the public site
(https://honua.io/data/customer-install-manifest.json) and drives the Windows/Linux customer
guides. It duplicates pins that live in platform-manifest.yaml, so without a gate its server
identity, qualification flags or Honua client pins could be malformed or drift while every other
release check stays green. Per AGENTS.md: a gate that can't fail is worse than no gate. The tests
in tools/test_validate_customer_install_manifest.py prove each rule below reddens.

  STRUCTURE   — the file parses as JSON with no duplicate keys and validates against
                schemas/customer-install-manifest.v1.schema.json (schema version, status, the two
                qualification flags, server identity, client identities, supporting images).

  QUALIFICATION — a pre-cut rehearsal claims neither qualification; clean-Windows qualification
                requires exact-candidate qualification; a release-candidate profile must be the
                exact certified candidate.

  SERVER      — the image is the manifest's honua-server repository pinned by digest, and its
                registry manifest URL names that same digest. The rehearsal server may differ from
                components.honua-server (an explicit, immutable rehearsal binding), but never
                half-match it: the same digest with another source commit, or the same commit with
                another digest, is drift. Exact-candidate qualification requires both to match, and
                any compatibility-ledger platform lock naming the digest must agree on the commit.

  CLIENTS     — every Honua client (and any client whose package is pinned in clientArtifacts)
                matches exactly one clientArtifacts entry: version, wheel/package digest or npm
                integrity, filename, repository and source commit. PyPI download and metadata URLs
                name the pinned file and version.

Usage:
  python tools/validate_customer_install_manifest.py
  python tools/validate_customer_install_manifest.py path/to/customer-install-manifest.json \\
      --platform-manifest platform-manifest.yaml --ledger compatibility-ledger.v1.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml  # PyYAML
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("PyYAML and jsonschema are required: pip install pyyaml jsonschema") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_MANIFEST_PATH = REPO_ROOT / "customer-install-manifest.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "customer-install-manifest.v1.schema.json"
PLATFORM_MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"
LEDGER_PATH = REPO_ROOT / "compatibility-ledger.v1.yaml"

SERVER_COMPONENT = "honua-server"
# Identity fields copied from clientArtifacts. A copied field must equal its pin exactly.
COPIED_CLIENT_FIELDS = ("ecosystem", "package", "version", "digest", "integrity", "filename",
                        "repository", "sourceSha", "publicationState", "registry", "targets")
# A Honua client must carry enough of the pin to identify its bytes and source on its own.
REQUIRED_HONUA_CLIENT_FIELDS = ("version", "repository", "sourceSha")


class ManifestLoadError(Exception):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestLoadError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_customer_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestLoadError(f"{path}: {exc}") from exc
    except ManifestLoadError as exc:
        raise ManifestLoadError(f"{path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestLoadError(f"{path}: top level must be a JSON object")
    return document


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestLoadError(f"{path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestLoadError(f"{path}: top level must be a mapping")
    return document


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def check_schema(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    return [f"{_json_path(error.absolute_path)}: {error.message}"
            for error in sorted(validator.iter_errors(document), key=lambda e: (list(map(str, e.absolute_path)), e.message))]


def is_honua_package(package: str) -> bool:
    lowered = package.lower()
    return lowered.startswith(("honua-", "honua_", "honua.", "@honua/"))


def _image_repository(reference: str) -> str:
    """Repository part of `repo:tag`, `repo@digest` or `repo:tag@digest`."""
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(last)] + last.split(":", 1)[0]


def check_qualification(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = document.get("status")
    exact = document.get("exactCandidateQualification")
    clean_windows = document.get("cleanWindowsQualification")
    if clean_windows is True and exact is not True:
        errors.append("$.cleanWindowsQualification: true requires exactCandidateQualification true "
                      "(clean-Windows qualification is against the signed exact candidate)")
    if status == "pre-cut-rehearsal" and (exact is True or clean_windows is True):
        errors.append("$.status: a pre-cut-rehearsal profile cannot claim exact-candidate or clean-Windows qualification")
    if status == "release-candidate" and exact is not True:
        errors.append("$.status: a release-candidate profile requires exactCandidateQualification true")
    verified_at = document.get("verifiedAt")
    if isinstance(verified_at, str):
        try:
            date.fromisoformat(verified_at)
        except ValueError:
            errors.append(f"$.verifiedAt: {verified_at!r} is not a calendar date")
    return errors


def _ledger_server_revisions(ledger: dict[str, Any] | None, digest: str) -> set[str]:
    revisions: set[str] = set()
    for record in ((ledger or {}).get("platformLocks") or {}).values():
        component = (((record or {}).get("platformLock") or {}).get("components") or {}).get(SERVER_COMPONENT) or {}
        for artifact in component.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("kind") == "image" and artifact.get("digest") == digest:
                revisions.add(str((component.get("source") or {}).get("revision")))
    return revisions


def check_server(document: dict[str, Any], platform: dict[str, Any], ledger: dict[str, Any] | None) -> list[str]:
    server = document.get("server")
    if not isinstance(server, dict):
        return []  # reported by the schema
    candidate = ((platform.get("components") or {}).get(SERVER_COMPONENT)) or {}
    candidate_image, candidate_digest, candidate_sha = (str(candidate.get(key) or "") for key in ("image", "digest", "sha"))
    if not (candidate_image and candidate_digest and candidate_sha):
        return [f"platform-manifest.yaml: components.{SERVER_COMPONENT} must pin image, digest and sha"]

    errors: list[str] = []
    image = str(server.get("image") or "")
    source_sha = str(server.get("sourceSha") or "")
    if "@" not in image:
        return errors  # reported by the schema
    digest = image.split("@", 1)[1]
    repository = _image_repository(image)
    expected_repository = _image_repository(candidate_image)
    if repository != expected_repository:
        errors.append(f"$.server.image: repository {repository!r} differs from platform-manifest.yaml "
                      f"components.{SERVER_COMPONENT}.image repository {expected_repository!r}")
    registry, _, path = repository.partition("/")
    expected_url = f"https://{registry}/v2/{path}/manifests/{digest}"
    if server.get("manifestUrl") != expected_url:
        errors.append(f"$.server.manifestUrl: must be {expected_url!r} (the registry manifest of the pinned digest), "
                      f"found {server.get('manifestUrl')!r}")

    same_digest = digest == candidate_digest
    same_sha = source_sha == candidate_sha
    if same_digest != same_sha:
        errors.append(
            f"$.server: half-matches the certified candidate — image digest {digest} "
            f"{'equals' if same_digest else 'differs from'} components.{SERVER_COMPONENT}.digest {candidate_digest} but "
            f"sourceSha {source_sha} {'equals' if same_sha else 'differs from'} components.{SERVER_COMPONENT}.sha {candidate_sha}")
    if document.get("exactCandidateQualification") is True and not (same_digest and same_sha):
        errors.append(f"$.exactCandidateQualification: true but the server is not the certified candidate "
                      f"components.{SERVER_COMPONENT} ({candidate_digest} @ {candidate_sha})")

    revisions = _ledger_server_revisions(ledger, digest)
    for revision in sorted(revisions - {source_sha}):
        errors.append(f"$.server.sourceSha: {source_sha} disagrees with compatibility-ledger platform lock "
                      f"{SERVER_COMPONENT} source revision {revision} for digest {digest}")
    if document.get("status") == "release-candidate" and ledger is not None and not revisions:
        errors.append(f"$.server.image: release-candidate digest {digest} is not recorded in any "
                      "compatibility-ledger platform lock")
    return errors


def _match_client_artifact(section: str, key: str, client: dict[str, Any],
                           artifacts: dict[str, Any]) -> tuple[str | None, list[str]]:
    path = f"$.{section}.{key}"
    pin_source = client.get("pinSource")
    if isinstance(pin_source, str) and "#clientArtifacts." in pin_source:
        name = pin_source.split("#clientArtifacts.", 1)[1]
        if name not in artifacts:
            return None, [f"{path}.pinSource: platform-manifest.yaml has no clientArtifacts.{name}"]
        return name, []
    matches = sorted(name for name, pin in artifacts.items()
                     if isinstance(pin, dict)
                     and pin.get("ecosystem") == client.get("ecosystem")
                     and pin.get("package") == client.get("package"))
    if len(matches) > 1:
        return None, [f"{path}: {client.get('ecosystem')}:{client.get('package')} matches several clientArtifacts "
                      f"entries {matches}; add an explicit pinSource"]
    return (matches[0] if matches else None), []


def _check_pypi_urls(path: str, client: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    package, version, filename = client.get("package"), client.get("version"), client.get("filename")
    download = client.get("downloadUrl")
    if isinstance(download, str) and isinstance(filename, str):
        if Path(urlparse(download).path).name != filename:
            errors.append(f"{path}.downloadUrl: does not download {filename!r}")
    if isinstance(package, str) and isinstance(version, str):
        expected = f"https://pypi.org/pypi/{package}/{version}/json"
        if "metadataUrl" in client and client["metadataUrl"] != expected:
            errors.append(f"{path}.metadataUrl: must be {expected!r}, found {client['metadataUrl']!r}")
        if isinstance(filename, str):
            stem = f"{package.replace('-', '_').replace('.', '_')}-{version}-".lower()
            if not filename.lower().startswith(stem):
                errors.append(f"{path}.filename: {filename!r} is not a {package} {version} wheel")
    return errors


def check_clients(document: dict[str, Any], platform: dict[str, Any]) -> list[str]:
    artifacts = platform.get("clientArtifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return ["platform-manifest.yaml: clientArtifacts must be a non-empty mapping"]
    errors: list[str] = []
    seen: dict[tuple[Any, Any], str] = {}
    for section in ("clients", "alternativeClients"):
        entries = document.get(section)
        if not isinstance(entries, dict):
            continue  # reported by the schema
        for key, client in entries.items():
            if not isinstance(client, dict):
                continue  # reported by the schema
            path = f"$.{section}.{key}"
            coordinate = (client.get("ecosystem"), client.get("package"))
            if coordinate in seen:
                errors.append(f"{path}: {coordinate[0]}:{coordinate[1]} is already listed at {seen[coordinate]}")
            seen.setdefault(coordinate, path)
            if client.get("ecosystem") == "pypi":
                errors.extend(_check_pypi_urls(path, client))

            name, match_errors = _match_client_artifact(section, key, client, artifacts)
            errors.extend(match_errors)
            if match_errors:
                continue
            honua = is_honua_package(str(client.get("package") or ""))
            if name is None:
                if honua:
                    errors.append(f"{path}: Honua client {coordinate[0]}:{coordinate[1]} is not pinned in "
                                  "platform-manifest.yaml clientArtifacts")
                continue
            pin = artifacts[name]
            for field in REQUIRED_HONUA_CLIENT_FIELDS if honua else ():
                if field not in client:
                    errors.append(f"{path}.{field}: required for a Honua client "
                                  f"(clientArtifacts.{name}.{field} = {pin.get(field)!r})")
            byte_field = "integrity" if pin.get("integrity") is not None else "digest"
            if pin.get(byte_field) is not None and byte_field not in client:
                errors.append(f"{path}.{byte_field}: required to pin package bytes "
                              f"(clientArtifacts.{name}.{byte_field} = {pin.get(byte_field)!r})")
            if client.get("ecosystem") == "pypi" and pin.get("filename") is not None and "filename" not in client:
                errors.append(f"{path}.filename: required for an exact wheel pin "
                              f"(clientArtifacts.{name}.filename = {pin.get('filename')!r})")
            for field in COPIED_CLIENT_FIELDS:
                if field in client and client[field] != pin.get(field):
                    errors.append(f"{path}.{field}: {client[field]!r} drifted from platform-manifest.yaml "
                                  f"clientArtifacts.{name}.{field} {pin.get(field)!r}")
    return errors


def validate(document: dict[str, Any], schema: dict[str, Any], platform: dict[str, Any],
             ledger: dict[str, Any] | None) -> list[str]:
    errors = check_schema(document, schema)
    errors += check_qualification(document)
    errors += check_server(document, platform, ledger)
    errors += check_clients(document, platform)
    return list(dict.fromkeys(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, nargs="?", default=CUSTOMER_MANIFEST_PATH)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--platform-manifest", type=Path, default=PLATFORM_MANIFEST_PATH)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    args = parser.parse_args(argv)
    try:
        document = load_customer_manifest(args.manifest)
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        platform = _load_yaml(args.platform_manifest)
        ledger = _load_yaml(args.ledger)
    except (ManifestLoadError, OSError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    errors = validate(document, schema, platform, ledger)
    if errors:
        print(f"REFUSED: {len(errors)} customer install manifest violation(s) in {args.manifest}", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"PASS: {args.manifest} is well-formed and every copied Honua identity matches {args.platform_manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
