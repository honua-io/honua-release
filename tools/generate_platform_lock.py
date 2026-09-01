#!/usr/bin/env python3
"""Generate a platform-lock.v1 draft and report every field the release must resolve.

The generator deliberately does not invent registry metadata or convert source snapshots into
released artifact identities. It writes the honest partial draft, then exits non-zero when its
worklist is non-empty.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]+\.[0-9]+(?:-rc\.[1-9][0-9]*)?$")


@dataclass
class Draft:
    lock: dict[str, Any]
    unresolved: list[str] = field(default_factory=list)


def _load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return value


def _file_identity(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"}


def _artifact_seed(component: dict[str, Any]) -> dict[str, Any] | None:
    coordinate = component.get("artifact")
    image = component.get("image")
    if image:
        return {"kind": "image", "coordinate": str(image).rsplit(":", 1)[0]}
    if not coordinate:
        return None
    prefix, _, name = str(coordinate).partition(":")
    kinds = {"npm": "npm", "nuget": "nuget", "pypi": "wheel", "oci-chart": "oci-chart", "terraform-registry": "terraform"}
    return {"kind": kinds.get(prefix, "other"), "coordinate": name or str(coordinate)}


def generate(manifest_path: Path, matrix_path: Path) -> Draft:
    manifest, matrix = _load(manifest_path), _load(matrix_path)
    release = str(manifest.get("platformRelease", ""))
    platform_id = f"honua-{release}" if PLATFORM_RELEASE_RE.fullmatch(release) else None
    release_notes = manifest.get("artifacts", {}).get("releaseNotes") if isinstance(manifest.get("artifacts"), dict) else None
    lock: dict[str, Any] = {
        "lockVersion": "platform-lock.v1",
        "platform": {"id": platform_id, "status": manifest.get("status")},
        "sourceInputs": {
            "platformManifest": _file_identity(manifest_path),
            "compatibilityMatrix": _file_identity(matrix_path),
        },
        "components": {},
        "contentDigests": {},
        "fixtures": [],
        "sbom": [],
        "provenance": [],
    }
    if isinstance(release_notes, str) and "tbd" not in release_notes.lower():
        lock["notes"] = release_notes
    unresolved: list[str] = []
    if not platform_id:
        unresolved.append(
            "$.platform.id: platformManifest.platformRelease is absent or not strict "
            "YYYY.N.P[-rc.N]; refusing to infer the missing identity"
        )
    unresolved.append("$.platform.supportTier: not declared by either source input")

    combined = list((manifest.get("components") or {}).items()) + list((manifest.get("experimental") or {}).items())
    for name, component in combined:
        cpath = f"$.components.{name}"
        entry: dict[str, Any] = {
            "source": {"revision": component.get("sha")},
            "contractVersions": component.get("contractVersions") or {},
            "schemaVersions": {},
            "artifacts": [],
        }
        if component.get("dbSchema") is not None:
            entry["schemaVersions"]["database"] = str(component["dbSchema"])
        seed = _artifact_seed(component)
        if seed:
            entry["artifacts"].append(seed)
        lock["components"][name] = entry
        unresolved.extend([
            f"{cpath}.source.repository: not declared by platform manifest",
            f"{cpath}.lifecycleStatus: exact GA/Preview/Experimental/Excluded status is not declared",
            f"{cpath}.supportTier: not declared",
        ])
        if not component.get("contractVersions"):
            unresolved.append(f"{cpath}.contractVersions: not declared")
        if not entry["schemaVersions"]:
            unresolved.append(f"{cpath}.schemaVersions: not declared")
        if not seed:
            unresolved.append(f"{cpath}.artifacts: no artifact coordinate is declared")
        else:
            apath = f"{cpath}.artifacts[0]"
            version = component.get("version")
            if version and version != "pre-release":
                seed["version"] = str(version)
            else:
                unresolved.append(f"{apath}.version: source snapshot/pre-release is not a released artifact version")
            unresolved.append(f"{apath}.sourceRevision: registry provenance must bind the artifact to its source revision")
            if seed["kind"] == "npm":
                unresolved.append(f"{apath}.integrity: npm registry integrity is not declared")
            elif seed["kind"] in ("nuget", "wheel"):
                unresolved.append(f"{apath}.sha256: package hash is not declared")
            elif seed["kind"] in ("image", "oci-chart"):
                digest = component.get("digest")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    seed["digest"] = digest
                else:
                    unresolved.append(f"{apath}.digest: immutable registry digest is not declared")
                unresolved.append(f"{apath}.architectures: registry architecture set is not declared")

    # The matrix is consumed for contract coherence, but it does not manufacture missing versions.
    for contract, body in (matrix.get("contracts") or {}).items():
        expected = str((body or {}).get("version", ""))
        if expected and not any(
            (component.get("contractVersions") or {}).get(contract) == expected
            for component in lock["components"].values()
        ):
            unresolved.append(f"$.components: compatibility contract {contract!r} version {expected!r} has no component declaration")
    unresolved.extend([
        "$.contentDigests.geospatialMcp: certified content digest is not declared",
        "$.contentDigests.catalog: catalog digest is not declared",
        "$.contentDigests.okf: OKF digest is not declared",
        "$.fixtures: fixture repository revisions are not declared",
        "$.sbom: immutable SBOM references and hashes are not declared",
        "$.provenance: immutable provenance references and hashes are not declared",
        "$.notes: immutable release-notes content/reference is not declared",
    ])
    return Draft(lock=lock, unresolved=unresolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "platform-manifest.yaml")
    parser.add_argument("--matrix", type=Path, default=REPO_ROOT / "compatibility-matrix.yaml")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "platform-lock.v1.draft.yaml")
    args = parser.parse_args(argv)
    try:
        draft = generate(args.manifest, args.matrix)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot generate lock draft: {exc}", file=sys.stderr)
        return 2
    args.output.write_text(yaml.safe_dump(draft.lock, sort_keys=False), encoding="utf-8")
    if draft.unresolved:
        print(f"BLOCKED: wrote {args.output}; {len(draft.unresolved)} release field(s) remain unresolved:", file=sys.stderr)
        for item in draft.unresolved:
            print(f"- {item}", file=sys.stderr)
        return 1
    print(f"PASS: wrote complete draft {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
