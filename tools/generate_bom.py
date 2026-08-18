#!/usr/bin/env python3
"""Generate the platform BOM (CycloneDX 1.5) from the pinned manifest.

The platform BOM is the customer-facing bill of materials for a `Honua YYYY.N` release: every shipped
component, its exact version + pinned commit + artifact/image, and the contract surfaces it speaks —
so a customer can scan precisely what they run (PLAN §6). Per-artifact SBOMs are emitted by each
component repo; this aggregates the *set* into one signed platform BOM attached to the release.

Deterministic: same manifest + label -> byte-identical BOM (the serial number is derived from a digest
of the components, the timestamp is passed in), so the BOM is reproducible and diff-able across cuts.

  build_bom(manifest, label, timestamp) -> dict     # pure, unit-tested
  python tools/generate_bom.py --label 2026.1 --timestamp 2026-07-01T00:00:00Z --out bom.cdx.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]

# How a manifest artifact-kind maps to a Package URL (purl) type + CycloneDX component type.
_PURL_TYPE = {"npm": "npm", "pypi": "pypi", "nuget": "nuget", "oci-chart": "oci",
              "terraform-registry": "terraform"}


def _base_label(label: str) -> str:
    return label.split("-rc")[0].strip()


def _purl(artifact: str, version: str) -> str | None:
    """Build a Package URL from a manifest `artifact` coordinate (e.g. 'npm:@honua/sdk-js')."""
    if not artifact or ":" not in artifact:
        return None
    kind, coord = artifact.split(":", 1)
    ptype = _PURL_TYPE.get(kind)
    if not ptype:
        return None
    # purl encodes '@' in a namespaced npm name as %40.
    name = coord.replace("@", "%40", 1) if coord.startswith("@") else coord
    ver = "" if (not version or version == "pre-release") else f"@{version}"
    return f"pkg:{ptype}/{name}{ver}"


def _component_entry(name: str, comp: dict) -> dict:
    version = str(comp.get("version", "")) or "unspecified"
    image = comp.get("image")
    artifact = comp.get("artifact")
    sha = str(comp.get("sha", ""))

    # type: a container image is a 'container'; a published package is a 'library'; otherwise (sha
    # pinned source like server/iac) 'application'.
    if image:
        ctype = "container"
    elif artifact:
        ctype = "library"
    else:
        ctype = "application"

    entry: dict = {"type": ctype, "name": name, "version": version, "bom-ref": f"{name}@{sha[:12] or version}"}
    purl = _purl(str(artifact or ""), version)
    if purl:
        entry["purl"] = purl

    props = []
    if sha:
        props.append({"name": "honua:vcs", "value": f"git+https://github.com/honua-io/{name}@{sha}"})
    if image:
        props.append({"name": "honua:image", "value": str(image)})
    for surface, ver in (comp.get("contractVersions") or {}).items():
        props.append({"name": f"honua:contract:{surface}", "value": str(ver)})
    if comp.get("dbSchema"):
        props.append({"name": "honua:dbSchema", "value": str(comp["dbSchema"])})
    if props:
        entry["properties"] = props
    return entry


def _serial_number(components: list[dict]) -> str:
    """A deterministic urn:uuid derived from the component set (reproducible per pinned set)."""
    digest = hashlib.sha256(json.dumps(components, sort_keys=True).encode()).hexdigest()
    h = digest[:32]
    # Format the first 32 hex chars as a UUID (version/variant bits left as-is; this is an identifier,
    # not a v4 UUID claim).
    return f"urn:uuid:{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def build_bom(manifest: dict, label: str, timestamp: str) -> dict:
    base = _base_label(label)
    components = [_component_entry(n, manifest["components"][n] or {})
                  for n in sorted(manifest.get("components") or {})]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": _serial_number(components),
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "type": "application",
                "name": "honua-platform",
                "version": base,
                "bom-ref": f"honua-platform@{base}",
                "description": f"Honua platform release {base} — certified, pinned component set.",
            },
            "properties": [{"name": "honua:platformRelease", "value": base}],
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--timestamp", required=True, help="ISO-8601 BOM timestamp")
    ap.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    bom = build_bom(manifest, args.label, args.timestamp)
    Path(args.out).write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    print(f"platform BOM ({len(bom['components'])} components) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
