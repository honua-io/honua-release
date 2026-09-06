#!/usr/bin/env python3
"""Resolve exact server/client compatibility from immutable ledger receipts."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import zipfile
import xml.etree.ElementTree as ET
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import release_inspect
from validate_platform_lock import validate as validate_lock


class CompatError(ValueError):
    pass


def resolve_server(value: str, timeout: float = 10.0) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    lock, _ = release_inspect.load_source(value, timeout)
    schema = json.loads(release_inspect.PLATFORM_LOCK_SCHEMA.read_text())
    schema_error = next(release_inspect.Draft202012Validator(schema).iter_errors(lock), None)
    if schema_error is not None:
        raise CompatError(f"invalid server platform lock: {schema_error.message}")
    findings = validate_lock(lock)
    if not findings.ok:
        raise CompatError(f"invalid server platform lock: {findings.errors[0]}")
    server = (lock.get("components") or {}).get("honua-server") or {}
    digests = {
        artifact["digest"]
        for artifact in server.get("artifacts") or []
        if artifact.get("kind") == "image" and artifact.get("digest")
    }
    if len(digests) != 1:
        raise CompatError(
            f"server endpoint resolved {len(digests)} honua-server image digests; expected exactly one"
        )
    return digests.pop()


def _coordinate(value: str) -> dict[str, str]:
    if "==" in value:
        name, version = value.rsplit("==", 1)
    elif "@" in value.lstrip("@"):  # preserves scoped npm names
        name, version = value.rsplit("@", 1)
    else:
        raise CompatError("client coordinate must be coordinate@version or coordinate==version")
    if not name or not version:
        raise CompatError("client coordinate and version must be non-empty")
    return {"coordinate": name, "identity": version}


def _local_package(path: Path) -> dict[str, str]:
    try:
        # Parse and hash the same bytes: reopening the path permits replacement
        # between identity inspection and digest calculation.
        payload = path.read_bytes()
        stream = io.BytesIO(payload)
        if path.suffix.lower() in (".nupkg", ".zip"):
            with zipfile.ZipFile(stream) as archive:
                member = _single([n for n in archive.namelist() if n.lower().endswith(".nuspec")])
                root = ET.fromstring(archive.read(member))
                metadata = _single([node for node in root if node.tag.split("}")[-1] == "metadata"])
                name = _single([node.text for node in metadata if node.tag.split("}")[-1] == "id"])
                version = _single([node.text for node in metadata if node.tag.split("}")[-1] == "version"])
        elif path.suffix.lower() == ".whl":
            with zipfile.ZipFile(stream) as archive:
                member = _single([n for n in archive.namelist() if n.endswith(".dist-info/METADATA")])
                metadata = BytesParser().parsebytes(archive.read(member))
                name = _single(metadata.get_all("Name", [None]))
                version = _single(metadata.get_all("Version", [None]))
        elif path.name.endswith((".tgz", ".tar.gz")):
            with tarfile.open(fileobj=stream, mode="r:*") as archive:
                member = _single([m for m in archive.getmembers() if m.name == "package/package.json" and m.isfile()])
                package = json.load(archive.extractfile(member))
                name, version = package["name"], package["version"]
        else:
            raise CompatError(f"unsupported local package type: {path.name}")
        if any(not isinstance(value, str) or not value.strip() for value in (name, version)):
            raise CompatError(f"cannot read package identity from {path}: Name and Version must be non-empty strings")
        return {"coordinate": name.strip(), "identity": version.strip(),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest()}
    except (OSError, KeyError, TypeError, UnicodeError, EOFError, ET.ParseError, zipfile.BadZipFile, tarfile.TarError, json.JSONDecodeError) as exc:
        raise CompatError(f"cannot inspect local package {path}: {exc}") from exc


def _single(values):
    if len(values) != 1:
        raise CompatError("package must contain exactly one unambiguous identity record")
    return values[0]


def resolve_client(value: str) -> dict[str, str]:
    path = Path(value)
    return _local_package(path) if path.is_file() else _coordinate(value)


def check(server_digest: str, client: dict[str, str], ledger: dict[str, Any]) -> dict[str, Any]:
    matches = [
        edge for edge in ledger.get("clientServerCertifications", [])
        if edge["serverDigest"] == server_digest
        and edge["client"]["coordinate"] == client["coordinate"]
        and edge["client"]["identity"] == client["identity"]
        and ("sha256" not in client or edge["client"].get("sha256") == client["sha256"])
    ]
    if len(matches) > 1:
        raise CompatError("ledger has multiple receipts for the exact server/client pair")
    edge = matches[0] if matches else None
    return {"status": edge["result"] if edge else "not-certified", "serverDigest": server_digest,
            "client": client, "receipt": edge["receipt"] if edge else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compat check", description=__doc__)
    parser.add_argument("server"); parser.add_argument("client")
    parser.add_argument("--ledger", type=Path, default=release_inspect.DEFAULT_LEDGER)
    parser.add_argument("--timeout", type=float, default=10.0); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check(resolve_server(args.server, args.timeout), resolve_client(args.client), release_inspect.load_ledger(args.ledger))
    except (CompatError, release_inspect.InspectError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr); return 2
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else result["status"].upper())
    return {"certified": 0, "not-certified": 1, "incompatible": 1}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
