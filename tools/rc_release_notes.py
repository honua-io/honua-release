#!/usr/bin/env python3
"""Render rc release notes: what ships, which gate passed when, what is not certified yet.

Honest by construction: the component table comes from platform-manifest.yaml (with the rc server
image passed explicitly, since rc.N may run ahead of the manifest pin), the gate table is the
rc_gate_window output, and the known-gaps list is read from a JSON array of open issues the caller
fetched. Nothing here claims certification the gate window did not record.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

NOT_CERTIFIED_RC = (
    "ArcGIS Pro / ArcPy and QGIS / PyQGIS client certification (operator ruling 2026-10-22: deferred past rc)",
    "EKS / Helm deployment (Preview, per the 2026-10-16 plan amendment)",
    "Multi-tenancy (internal only for 2026.1)",
    "Licensing enforcement (ships Disabled; the documented posture is `Licensing__Mode=Disabled`)",
)


def component_rows(manifest: dict, server_image: str | None, server_digest: str | None) -> list[str]:
    rows = []
    comps = manifest.get("components") or {}
    for name in sorted(comps):
        c = comps[name] or {}
        version = str(c.get("version") or "pre-release")
        sha = str(c.get("sha") or "")[:12]
        artifact = c.get("image") or c.get("artifact") or "—"
        if name == "honua-server" and server_image:
            artifact = server_image + (f" @ `{server_digest}`" if server_digest else "")
            sha = "see image tag"
        rows.append(f"| {name} | {version} | `{sha}` | {artifact} |")
    return rows


def render(manifest: dict, gate_md: str, label: str, server_image: str | None, server_digest: str | None,
           gaps: list[dict], intro: str | None = None) -> str:
    title = label.replace("-", " ")
    parts = [f"# Honua {title}\n"]
    parts.append(intro or (
        "A **release candidate** of the Honua platform: one cloud-native geospatial server that exposes "
        "PostGIS-backed data through GeoServices REST, OGC API, classic OGC WMS/WFS/WMTS/WCS, STAC, OData, "
        "vector tiles, 3D Tiles, gRPC and MCP for AI agents, with SDKs for JavaScript, Python and .NET, a CLI, "
        "and the Console. This candidate is for evaluation and feedback. It is admitted under the rc rule "
        "below, not the GA rule, and may contain defects; the known gaps are listed, not hidden."))
    parts.append("\n## Admission rule for this candidate\n")
    parts.append("Every release-train gate has at least one green run on `trunk` within the same 7-day window, "
                 "against any build (release#376 owner amendment, 2026-10-22). Gates were **not** required to agree on "
                 "one pinned digest; that is the GA rule, which 2026.1 GA converges toward.\n")
    parts.append("\n## Components\n\n| Component | Version | Pinned commit | Artifact / image |\n|---|---|---|---|")
    parts.extend(component_rows(manifest, server_image, server_digest))
    parts.append("\n## Gate record\n")
    parts.append(gate_md.rstrip() + "\n")
    parts.append("\n## Not certified in this candidate\n")
    parts.extend(f"- {item}" for item in NOT_CERTIFIED_RC)
    parts.append("\n## Known gaps (open at cut)\n")
    if gaps:
        parts.append("| Issue | Title |\n|---|---|")
        for g in sorted(gaps, key=lambda x: (x.get("repo", ""), x.get("number", 0))):
            parts.append(f"| [{g.get('repo')}#{g.get('number')}]({g.get('url')}) | {g.get('title', '').replace('|', '/')} |")
    else:
        parts.append("_None recorded._")
    parts.append("\n## Install\n")
    parts.append("Follow [Install Honua locally with Docker](https://honua.io/docs/playbooks/install-with-docker/) "
                 "with the server image above. Published SDKs: `@honua/sdk-js`, `honua-sdk` (PyPI), `Honua.Sdk` (NuGet) "
                 "at the versions in the component table.\n")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", default="platform-manifest.yaml")
    ap.add_argument("--gate-markdown", required=True)
    ap.add_argument("--label", required=True, help="e.g. 2026.1-rc.1")
    ap.add_argument("--server-image", default=None)
    ap.add_argument("--server-digest", default=None)
    ap.add_argument("--gaps-json", default=None, help="JSON array of {repo,number,title,url}")
    ap.add_argument("--intro-file", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8"))
    gate_md = Path(args.gate_markdown).read_text(encoding="utf-8")
    gaps = json.loads(Path(args.gaps_json).read_text(encoding="utf-8")) if args.gaps_json else []
    intro = Path(args.intro_file).read_text(encoding="utf-8") if args.intro_file else None
    Path(args.out).write_text(render(manifest, gate_md, args.label, args.server_image, args.server_digest, gaps, intro),
                              encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
