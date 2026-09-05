#!/usr/bin/env python3
"""Resolve exact server/client compatibility from immutable ledger receipts."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
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
        if path.suffix.lower() in (".nupkg", ".zip"):
            with zipfile.ZipFile(path) as archive:
                member = next(n for n in archive.namelist() if n.lower().endswith(".nuspec"))
                text = archive.read(member).decode("utf-8")
                name = re.search(r"<(?:\w+:)?id>([^<]+)</", text)
                version = re.search(r"<(?:\w+:)?version>([^<]+)</", text)
        elif path.suffix.lower() == ".whl":
            with zipfile.ZipFile(path) as archive:
                member = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(member))
                name, version = metadata["Name"], metadata["Version"]
                if not name or not name.strip() or not version or not version.strip():
                    raise CompatError(f"cannot read package identity from {path}: Name and Version must be non-empty")
                return {"coordinate": name, "identity": version}
        elif path.name.endswith((".tgz", ".tar.gz")):
            with tarfile.open(path, "r:*") as archive:
                member = next(m for m in archive.getmembers() if m.name.endswith("/package.json"))
                package = json.load(archive.extractfile(member))
                return {"coordinate": package["name"], "identity": package["version"]}
        else:
            raise CompatError(f"unsupported local package type: {path.name}")
        if not name or not version:
            raise CompatError(f"cannot read package identity from {path}")
        return {"coordinate": name.group(1), "identity": version.group(1)}
    except (OSError, KeyError, StopIteration, zipfile.BadZipFile, tarfile.TarError, json.JSONDecodeError) as exc:
        raise CompatError(f"cannot inspect local package {path}: {exc}") from exc


def resolve_client(value: str) -> dict[str, str]:
    path = Path(value)
    return _local_package(path) if path.is_file() else _coordinate(value)


def check(server_digest: str, client: dict[str, str], ledger: dict[str, Any]) -> dict[str, Any]:
    matches = [
        edge for edge in ledger.get("clientServerCertifications", [])
        if edge["serverDigest"] == server_digest
        and edge["client"]["coordinate"].casefold() == client["coordinate"].casefold()
        and edge["client"]["identity"] == client["identity"]
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
