"""Refresh the release narrative's client table from the platform manifest."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = (
    ("JavaScript / TypeScript", "honua-sdk-js", "npm"),
    ("JavaScript client compatibility", "honua-sdk-client-compat", "npm"),
    ("Python", "honua-sdk-python-wheel", "PyPI"),
    (".NET (net10.0)", "honua-sdk-dotnet", "NuGet"),
)


def update_table(narrative: str, manifest: dict) -> str:
    table = re.search(r"(?m)(^### Supported clients\n\n)(?:\|[^\n]*\n)+", narrative)
    if table is None:
        raise ValueError("Supported clients table is missing")
    rows = ["| SDK | Package | Version for 2026.1 |", "|---|---|---|"]
    for label, key, registry in CLIENTS:
        artifact = manifest["clientArtifacts"][key]
        rows.append(f"| {label} | `{artifact['package']}` ({registry}) | {artifact['version']} |")
    return narrative[:table.start()] + table.group(1) + "\n".join(rows) + "\n" + narrative[table.end():]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--narrative", type=Path, default=ROOT / "release-notes/2026.1.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    original = args.narrative.read_text(encoding="utf-8")
    updated = update_table(original, yaml.safe_load(args.manifest.read_text(encoding="utf-8")))
    if args.check:
        if original != updated:
            raise SystemExit("Supported clients table is stale")
    else:
        args.narrative.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
