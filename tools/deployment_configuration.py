#!/usr/bin/env python3
"""Verify and select retained deployment files for an immutable server image."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from check_protocol_certification import _DuplicateJsonKey, _unique_json_object
from digests import file_sha256_hex

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("release/deployment-configurations/catalog.v1.json")
SCHEMA = ROOT / "schemas/deployment-configurations.v1.schema.json"


def checked_file(root: Path, record: dict) -> str:
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("configuration path must be relative without parent traversal")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root / "release/deployment-configurations"):
        raise ValueError("configuration path escapes deployment-configurations")
    if not path.is_file():
        raise ValueError(f"configuration is not a regular file: {relative}")
    if file_sha256_hex(path) != record["sha256"]:
        raise ValueError(f"configuration checksum mismatch: {relative}")
    return str(path)


def verified_catalog(root: Path) -> dict:
    try:
        catalog = json.loads((root / CATALOG).read_text(encoding="utf-8"),
                             object_pairs_hook=_unique_json_object)
    except _DuplicateJsonKey as exc:
        raise ValueError(f"catalog contains duplicate JSON key {exc.args[0]!r}") from exc
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    try:
        validator.validate(catalog)
    except ValidationError as exc:
        raise ValueError(f"invalid deployment configuration catalog at {list(exc.absolute_path)}") from exc
    for entry in catalog["images"].values():
        for records in entry["configurations"].values():
            for record in records:
                checked_file(root, record)
    return catalog["images"]


def select(root: Path, image: str, kind: str) -> dict:
    images = verified_catalog(root)
    if image not in images:
        raise ValueError("image is not retained in the deployment configuration catalog")
    entry = images[image]
    if kind not in entry["configurations"]:
        raise ValueError(f"image has no retained {kind} configuration")
    return {
        "image": image,
        "release": entry["release"],
        "rollbackMode": entry["rollbackMode"],
        "source": entry["source"],
        "files": [checked_file(root, record) for record in entry["configurations"][kind]],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--image")
    parser.add_argument("--kind", choices=("compose", "helm"), default="compose")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if args.check:
            verified_catalog(root)
        else:
            print(json.dumps(select(root, args.image, args.kind), indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"DEPLOYMENT_CONFIGURATION_REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
