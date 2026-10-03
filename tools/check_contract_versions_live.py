#!/usr/bin/env python3
"""Live contract-version gate (ruling R27, honua-release#376).

honua-server declares its contract versions in `release/component-versions.json` at the candidate
sha (#231 WI-3a, docs/COMPONENT-VERSION-DECLARATIONS.md). That declaration is the input for the
lock's `contractVersions`. This gate checks it against the exact candidate image: the workflow
(gate-contract-live.yml) boots `image@digest`, saves the anonymous `/api/v1/admin/capabilities`
response, and this module compares every declared key with the advertised value.

  missing   a declared key the candidate does not advertise
  extra     an advertised key the declaration does not list
  mismatch  the same key with a different value

Any of the three refuses. The report carries both maps, so a refusal shows its own evidence.

What the envelope advertises: `data.compatibility.contractVersions`, when the server publishes that
map, is the advertised set as a whole. Without it, only the two fields the envelope carries today
are read: `adminApiMajor` (key `admin`) and `metadataApiVersion` (key `metadata`). Every other
declared key is then missing, because nothing at this endpoint advertises it.

Statuses: `pass`, `fail` (the candidate was read and disagrees with the declaration), and
`blocked` (no evidence: the declaration or the capabilities response is unreadable). The workflow
fails a strict run on `blocked`.

  python tools/check_contract_versions_live.py --manifest platform-manifest.yaml \
      --capabilities caps.json --image-ref ghcr.io/...@sha256:... --out report.json \
      [--declaration component-versions.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

GATE = "contract-live"
SERVER = "honua-server"
CAPABILITIES_PATH = "/api/v1/admin/capabilities"
# The envelope's flat compatibility fields that name a declared contract (the manifest's
# provenance comments record the same two reads).
ENVELOPE_FIELDS = (("admin", "adminApiMajor"), ("metadata", "metadataApiVersion"))


def advertised_contract_versions(document: object) -> dict[str, str]:
    """The contract versions an admin capabilities response advertises, or ValueError."""
    data = document.get("data") if isinstance(document, dict) else None
    compatibility = data.get("compatibility") if isinstance(data, dict) else None
    if not isinstance(compatibility, dict):
        raise ValueError(f"{CAPABILITIES_PATH} response has no data.compatibility object")
    explicit = compatibility.get("contractVersions")
    if explicit is not None:
        if not isinstance(explicit, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in explicit.items()
        ):
            raise ValueError("data.compatibility.contractVersions must map names to version strings")
        return dict(explicit)
    advertised = {}
    for key, field in ENVELOPE_FIELDS:
        value = compatibility.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"data.compatibility.{field} must be a string")
        advertised[key] = value
    return advertised


def compare(declared: dict[str, str], advertised: dict[str, str]) -> list[dict]:
    """Every disagreement between the two maps, one finding per key, sorted by key."""
    findings = []
    for key in sorted(declared.keys() | advertised.keys()):
        if key not in advertised:
            kind = "missing"
        elif key not in declared:
            kind = "extra"
        elif declared[key] != advertised[key]:
            kind = "mismatch"
        else:
            continue
        findings.append({"key": key, "kind": kind,
                         "declared": declared.get(key), "advertised": advertised.get(key)})
    return findings


def check(declared: dict[str, str], advertised: dict[str, str]) -> dict:
    findings = compare(declared, advertised)
    if findings:
        why = "advertised contract versions differ from the declaration: " + "; ".join(
            f"{f['key']} {f['kind']} (declared {f['declared']!r}, advertised {f['advertised']!r})"
            for f in findings)
    else:
        why = f"all {len(declared)} declared contract versions are advertised unchanged"
    return {"gate": GATE, "status": "fail" if findings else "pass", "why": why,
            "declared": dict(sorted(declared.items())), "advertised": dict(sorted(advertised.items())),
            "findings": findings}


class _DeclarationFile:
    """Serves one already-fetched declaration through the resolver's `github.file` seam."""

    def __init__(self, path: Path):
        self.raw = path.read_bytes()

    def file(self, repository, sha, path):
        return self.raw


def server_declaration(manifest: dict, declaration: Path | None = None) -> tuple[dict, dict[str, str]]:
    """(provenance, declared contractVersions) for the manifest's honua-server sha.

    The declaration is read and validated exactly as the nightly resolver reads it. Without
    `declaration` it is fetched at the pinned sha; with it, those bytes stand in for the fetch.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import resolve_trunk_candidate as resolver

    component = (manifest.get("components") or {}).get(SERVER)
    if not isinstance(component, dict):
        raise ValueError(f"manifest has no components.{SERVER}")
    source = _DeclarationFile(declaration) if declaration else resolver.GitHub()
    declared = resolver.component_versions(source, SERVER, component)["contractVersions"]
    repository = str(component.get("repository") or "").removeprefix("https://github.com/")
    provenance = {"repository": repository, "sha": component.get("sha"),
                  "path": resolver.COMPONENT_VERSIONS_PATH}
    return provenance, declared


def blocked(why: str) -> dict:
    return {"gate": GATE, "status": "blocked", "why": why, "declared": None, "advertised": None,
            "findings": []}


def run(manifest_path: Path, capabilities_path: Path, image_ref: str,
        declaration: Path | None = None) -> dict:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    try:
        provenance, declared = server_declaration(manifest, declaration)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:  # no evidence, never a pass
        report = blocked(f"declaration unreadable: {exc}")
        provenance = None
    else:
        try:
            advertised = advertised_contract_versions(
                json.loads(capabilities_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            report = blocked(f"{CAPABILITIES_PATH} unreadable: {exc}")
            report["declared"] = dict(sorted(declared.items()))
        else:
            report = check(declared, advertised)
    report["image"] = image_ref
    report["declaration"] = provenance
    report["capabilities"] = CAPABILITIES_PATH
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--capabilities", type=Path, required=True,
                        help=f"saved {CAPABILITIES_PATH} response body from the booted candidate")
    parser.add_argument("--image-ref", required=True, help="the image@digest that served it")
    parser.add_argument("--declaration", type=Path,
                        help="already-fetched component-versions.json (default: read at the pinned sha)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run(args.manifest, args.capabilities, args.image_ref, args.declaration)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{GATE}: {report['status'].upper()}: {report['why']}")
    return {"pass": 0, "fail": 1}.get(report["status"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
