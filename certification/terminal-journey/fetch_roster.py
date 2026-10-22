#!/usr/bin/env python3
"""Fetch honua-server's Admin parity exports at the source sha of the image under test.

Operator ruling on honua-release#785: the control-plane roster is data-driven. The journey reads
docs/gis/data/admin-openapi-operation-ids.json and admin-mcp-projection-manifest.json as honua-server
published them at the manifest-pinned server sha, never at a branch head. Prints the run.py arguments
(and, with --github-output, writes rest/mcp/sha outputs) so callers wire the exact files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RAW = "https://raw.githubusercontent.com/honua-io/honua-server/{sha}/{path}"
EXPORTS = {"rest": "docs/gis/data/admin-openapi-operation-ids.json",
           "mcp": "docs/gis/data/admin-mcp-projection-manifest.json"}


def fetch(sha: str, out: Path, opener=urllib.request.urlopen) -> dict[str, Path]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"honua-server sha must be a full commit sha, got {sha!r}")
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, path in EXPORTS.items():
        with opener(RAW.format(sha=sha, path=path), timeout=60) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise SystemExit(f"{path} exceeds the 16 MiB bound")
        json.loads(raw)  # refuse non-JSON before any caller reads it
        target = out / Path(path).name
        target.write_bytes(raw)
        paths[key] = target
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    sha = str(yaml.safe_load(args.manifest.read_text())["components"]["honua-server"]["sha"])
    paths = fetch(sha, args.out)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"rest={paths['rest']}\nmcp={paths['mcp']}\nsha={sha}\n")
    print(f"--rest-roster {paths['rest']} --mcp-roster {paths['mcp']} --roster-source-sha {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
