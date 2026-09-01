#!/usr/bin/env python3
"""Resolve a platform lock identity and list its receipt-backed certifications."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "compatibility-ledger.v1.yaml"
LEDGER_SCHEMA = REPO_ROOT / "schemas/compatibility-ledger.v1.schema.json"
PLATFORM_LOCK_SCHEMA = REPO_ROOT / "schemas/platform-lock.v1.schema.json"
WELL_KNOWN_PATH = "/.well-known/honua/platform-lock"


class InspectError(ValueError):
    pass


def canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(payload: bytes, source: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(payload)
    except yaml.YAMLError as exc:
        raise InspectError(f"{source}: invalid YAML/JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InspectError(f"{source}: expected a YAML/JSON mapping")
    return value


def load_source(source: str, timeout: float = 10.0) -> tuple[dict[str, Any], str]:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in ("http", "https"):
        url = source
        if not parsed.path or parsed.path == "/":
            url = source.rstrip("/") + WELL_KNOWN_PATH
        request = urllib.request.Request(url, headers={"Accept": "application/yaml, application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _mapping(response.read(), url), url
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise InspectError(f"{url}: cannot retrieve platform lock: {exc}") from exc
    path = Path(source)
    try:
        return _mapping(path.read_bytes(), str(path)), str(path)
    except OSError as exc:
        raise InspectError(f"{path}: cannot read platform lock: {exc}") from exc


def load_ledger(path: Path) -> dict[str, Any]:
    try:
        ledger = _mapping(path.read_bytes(), str(path))
    except OSError as exc:
        raise InspectError(f"{path}: cannot read compatibility ledger: {exc}") from exc
    ledger_schema = json.loads(LEDGER_SCHEMA.read_text())
    lock_schema = json.loads(PLATFORM_LOCK_SCHEMA.read_text())
    registry = Registry().with_resource(lock_schema["$id"], Resource.from_contents(lock_schema))
    schema_errors = sorted(
        Draft202012Validator(ledger_schema, registry=registry).iter_errors(ledger),
        key=lambda error: list(error.absolute_path),
    )
    if schema_errors:
        error = schema_errors[0]
        location = "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        raise InspectError(f"{path}: invalid compatibility ledger at {location}: {error.message}")
    # Imported here to keep the validator reusable without a module import cycle.
    from validate_compatibility_ledger import validate
    errors = validate(ledger)
    if errors:
        raise InspectError(f"{path}: incoherent compatibility ledger: {errors[0]}")
    return ledger


def _artifact_identity(artifact: dict[str, Any]) -> str:
    for key in ("digest", "sha256", "integrity", "version"):
        if artifact.get(key):
            return str(artifact[key])
    return "unresolved"


def inspect(lock: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    if lock.get("lockVersion") != "platform-lock.v1":
        raise InspectError("input is not a platform-lock.v1 manifest")
    digest = canonical_digest(lock)
    record = (ledger.get("platformLocks") or {}).get(digest)
    platform = lock.get("platform") or {}
    components = []
    for name, component in sorted((lock.get("components") or {}).items()):
        artifacts = []
        for artifact in component.get("artifacts") or []:
            artifacts.append({
                "coordinate": artifact.get("coordinate"),
                "identity": _artifact_identity(artifact),
                "sourceRevision": artifact.get("sourceRevision"),
            })
        components.append({
            "name": name,
            "sourceRevision": (component.get("source") or {}).get("revision"),
            "lifecycleStatus": component.get("lifecycleStatus"),
            "artifacts": artifacts,
        })
    receipts = [] if not record else [r for r in record.get("certifications", []) if isinstance(r, dict) and r]
    server_digests = {
        artifact.get("digest")
        for component in (lock.get("components") or {}).values()
        for artifact in component.get("artifacts") or []
        if artifact.get("kind") == "image" and artifact.get("digest")
    }
    pair_receipts = [
        edge for edge in ledger.get("clientServerCertifications", [])
        if edge.get("serverDigest") in server_digests and isinstance(edge.get("receipt"), dict) and edge["receipt"]
    ]
    exclusions = [
        edge for edge in ledger.get("experimentalExclusions", []) if edge.get("lockDigest") == digest
    ]
    return {
        "platform": platform.get("id"),
        "lockDigest": digest,
        "ledgerRecord": "resolved" if record else "absent",
        "certified": bool(receipts),
        "certifications": receipts,
        "clientServerCertifications": pair_receipts,
        "experimentalExclusions": exclusions,
        "components": components,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"platform: {result['platform']}",
        f"platform-lock: {result['lockDigest']}",
        f"ledger-record: {result['ledgerRecord']}",
        f"certified: {'yes' if result['certified'] else 'no (no receipt)'}",
        "identity-chain:",
    ]
    for component in result["components"]:
        lines.append(f"  {component['name']} @ {component['sourceRevision']} [{component['lifecycleStatus']}]")
        for artifact in component["artifacts"]:
            lines.append(f"    -> {artifact['coordinate']} @ {artifact['identity']} (source {artifact['sourceRevision']})")
    lines.append("certifications:")
    if not result["certifications"]:
        lines.append("  none — not certified")
    for receipt in result["certifications"]:
        lines.append(f"  {receipt['schema']} {receipt['sha256']} {receipt['uri']}")
    lines.append("server-client certifications:")
    if not result["clientServerCertifications"]:
        lines.append("  none — no receipt")
    for edge in result["clientServerCertifications"]:
        client, receipt = edge["client"], edge["receipt"]
        lines.append(f"  {edge['serverDigest']} + {client['coordinate']}@{client['identity']} -> {receipt['sha256']}")
    lines.append("experimental exclusions:")
    if not result["experimentalExclusions"]:
        lines.append("  none")
    for exclusion in result["experimentalExclusions"]:
        lines.append(f"  {exclusion['component']}: {exclusion['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release inspect", description=__doc__)
    parser.add_argument("manifest_or_endpoint")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        lock, source = load_source(args.manifest_or_endpoint, args.timeout)
        result = inspect(lock, load_ledger(args.ledger))
    except InspectError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    result["source"] = source
    print(json.dumps(result, indent=2, sort_keys=True) if args.as_json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
