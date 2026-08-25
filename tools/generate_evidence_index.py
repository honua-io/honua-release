#!/usr/bin/env python3
"""Emit the manifest's package/source provenance for release-evidence consumers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_index(manifest: dict) -> dict:
    clients = []
    for name, value in sorted((manifest.get("clientArtifacts") or {}).items()):
        clients.append({"name": name, **(value or {})})
    sources = []
    for name, value in sorted((manifest.get("evidenceSources") or {}).items()):
        sources.append({"name": name, **(value or {})})
    return {
        "schemaVersion": "honua.release-evidence-pins.v1",
        "platformRelease": manifest.get("platformRelease"),
        "clientArtifacts": clients,
        "evidenceSources": sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    output = Path(args.out)
    output.write_text(json.dumps(build_index(manifest), indent=2) + "\n", encoding="utf-8")
    print(f"release evidence pin index -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
