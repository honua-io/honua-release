#!/usr/bin/env python3
"""Verify and select retained deployment files for an immutable server image."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from digests import file_sha256_hex

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("deployment-configurations/catalog.v1.json")
IMAGE = re.compile(r"ghcr\.io/honua-io/honua-server@sha256:[0-9a-f]{64}")


def checked_file(root: Path, record: dict) -> str:
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("configuration path must be relative without parent traversal")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root / "deployment-configurations"):
        raise ValueError("configuration path escapes deployment-configurations")
    expected = record["sha256"]
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("configuration requires a SHA-256 checksum")
    if not path.is_file() or file_sha256_hex(path) != expected:
        raise ValueError(f"configuration checksum mismatch: {relative}")
    return str(path)


def verified_catalog(root: Path) -> dict:
    catalog = json.loads((root / CATALOG).read_text(encoding="utf-8"))
    if catalog["schema"] != "honua.deployment-configurations/v1":
        raise ValueError("unsupported deployment configuration catalog schema")
    images = catalog["images"]
    if not isinstance(images, dict) or not images:
        raise ValueError("deployment configuration catalog has no images")
    for image, entry in images.items():
        if not IMAGE.fullmatch(image):
            raise ValueError("catalog keys must be immutable server image references")
        if entry["rollbackMode"] not in {"manual", "paired"}:
            raise ValueError("unsupported rollback mode")
        configurations = entry["configurations"]
        if not isinstance(configurations, dict) or not configurations:
            raise ValueError("image has no deployment configurations")
        for kind, records in configurations.items():
            if kind not in {"compose", "helm"} or not isinstance(records, list) or not records:
                raise ValueError("unsupported or empty deployment configuration")
            for record in records:
                checked_file(root, record)
    return images


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
