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
PLATFORM_RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]+(?:-rc\.[1-9][0-9]*)?$")
LIFECYCLE_STATUSES = {"GA", "Preview", "Experimental", "Excluded"}


@dataclass
class Draft:
    lock: dict[str, Any]
    unresolved: list[str] = field(default_factory=list)
    deferred_until_cut: list[str] = field(default_factory=list)


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
    kinds = {
        "npm": "npm", "nuget": "nuget", "pypi": "wheel", "oci-chart": "oci-chart",
        "terraform-registry": "terraform", "spec": "spec", "archive": "archive",
    }
    return {"kind": kinds.get(prefix, "other"), "coordinate": name or str(coordinate)}


def generate(manifest_path: Path, matrix_path: Path) -> Draft:
    manifest, matrix = _load(manifest_path), _load(matrix_path)
    release = str(manifest.get("platformRelease", ""))
    platform_id = f"honua-{release}" if PLATFORM_RELEASE_RE.fullmatch(release) else None
    release_notes = manifest.get("artifacts", {}).get("releaseNotes") if isinstance(manifest.get("artifacts"), dict) else None
    lock: dict[str, Any] = {
        "lockVersion": "platform-lock.v1",
        "platform": {"id": platform_id, "status": manifest.get("status"), "supportTier": "ga"},
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
    deferred: list[str] = []

    def refuse(message: str, resolution: str) -> None:
        rendered = f"[{resolution}] {message}"
        unresolved.append(rendered)
        if resolution == "AT-CUT":
            deferred.append(rendered)
    if not platform_id:
        unresolved.append(
            "$.platform.id: platformManifest.platformRelease is absent or not strict "
            "YYYY.N[-rc.N]; refusing to infer the missing identity"
        )
    combined = list((manifest.get("components") or {}).items()) + list((manifest.get("experimental") or {}).items())
    for name, component in combined:
        cpath = f"$.components.{name}"
        entry: dict[str, Any] = {
            "source": {"repository": component.get("repository"), "revision": component.get("sha")},
            "contractVersions": component.get("contractVersions") or {},
            "schemaVersions": {},
            "artifacts": [],
            "artifactIdentityModel": "source-pinned" if component.get("sourcePinnedOnly") else "published",
        }
        if component.get("dbSchema") is not None:
            entry["schemaVersions"]["database"] = str(component["dbSchema"])
        seed = _artifact_seed(component)
        if seed:
            entry["artifacts"].append(seed)
        lock["components"][name] = entry
        if not component.get("repository"):
            refuse(f"{cpath}.source.repository: not declared by platform manifest", "MECHANICAL")
        lifecycle_status = component.get("lifecycleStatus")
        if lifecycle_status is None and component.get("status") == "experimental":
            lifecycle_status = "Experimental"
        if lifecycle_status in LIFECYCLE_STATUSES:
            entry["lifecycleStatus"] = lifecycle_status
            entry["supportTier"] = lifecycle_status.lower()
        else:
            refuse(f"{cpath}.lifecycleStatus: exact GA/Preview/Experimental/Excluded status is not declared", "DECISION")
        if not component.get("contractVersions"):
            resolution = "PUBLISH" if name in {"honua-sdk-dotnet", "honua-sdk-js", "honua-sdk-python"} else "AT-CUT"
            refuse(f"{cpath}.contractVersions: not declared", resolution)
        if not entry["schemaVersions"]:
            refuse(f"{cpath}.schemaVersions: not declared", "AT-CUT")
        if not seed and not component.get("sourcePinnedOnly"):
            refuse(f"{cpath}.artifacts: no artifact coordinate is declared", "DECISION")
        elif seed:
            apath = f"{cpath}.artifacts[0]"
            version = component.get("artifactVersion") or component.get("version")
            if version and version != "pre-release":
                seed["version"] = str(version)
            else:
                resolution = "AT-CUT" if name == "honua-server" else ("PUBLISH" if name in {"honua-console", "honua-helm"} else "DECISION")
                refuse(f"{apath}.version: source snapshot/pre-release is not a released artifact version", resolution)
            artifact_revision = component.get("artifactSourceRevision")
            if artifact_revision:
                seed["sourceRevision"] = artifact_revision
            if seed["kind"] == "npm":
                published = (manifest.get("clientArtifacts") or {}).get(name) or {}
                if published.get("integrity"):
                    seed["integrity"] = published["integrity"]
                    seed["sourceRevision"] = published.get("sourceSha")
                else:
                    refuse(f"{apath}.integrity: npm registry integrity is not declared", "MECHANICAL")
            elif seed["kind"] in ("nuget", "wheel"):
                published_name = "honua-sdk-python-wheel" if name == "honua-sdk-python" else name
                published = {} if name == "honua-sdk-dotnet" else (manifest.get("clientArtifacts") or {}).get(published_name) or {}
                digest = component.get("artifactSha256") or published.get("digest")
                if digest and (component.get("artifactSourceRevision") or published.get("sourceSha")):
                    seed["sha256"] = digest
                    seed["sourceRevision"] = component.get("artifactSourceRevision") or published.get("sourceSha")
                else:
                    refuse(f"{apath}.sha256: package hash is not declared", "PUBLISH" if name == "honua-sdk-dotnet" else "MECHANICAL")
            elif seed["kind"] in ("spec", "archive"):
                digest = component.get("artifactSha256")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    seed["sha256"] = digest
                else:
                    refuse(f"{apath}.sha256: artifact hash is not declared", "MECHANICAL")
            elif seed["kind"] in ("image", "oci-chart"):
                digest = component.get("digest")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    seed["digest"] = digest
                else:
                    refuse(f"{apath}.digest: immutable registry digest is not declared", "PUBLISH")
                image_architectures = component.get("imageArchitectures")
                if seed["kind"] == "image" and isinstance(image_architectures, dict) and image_architectures:
                    seed["architectures"] = sorted(image_architectures)
                    seed["architectureDigests"] = {
                        architecture: details.get("digest")
                        for architecture, details in sorted(image_architectures.items())
                        if isinstance(details, dict)
                    }
                    fargate = (((matrix.get("deploy") or {}).get(name) or {}).get("awsFargate") or {})
                    compatibility = fargate.get("architectures") or {}
                    seed["awsFargateArchitectures"] = {
                        architecture: details
                        for architecture, details in sorted(compatibility.items())
                        if isinstance(details, dict)
                    }
                    for architecture in seed["architectures"]:
                        status = (compatibility.get(architecture) or {}).get("status")
                        if status not in {"certified", "excluded"}:
                            refuse(
                                f"{apath}.awsFargateArchitectures.{architecture}: compatibility matrix must declare certified or excluded",
                                "DECISION",
                            )
                    for architecture in sorted(set(compatibility) - set(seed["architectures"])):
                        refuse(
                            f"{apath}.awsFargateArchitectures.{architecture}: compatibility declared for an unpublished architecture",
                            "MECHANICAL",
                        )
                else:
                    refuse(f"{apath}.architectures: registry architecture set is not declared", "AT-CUT" if name == "honua-server" else "PUBLISH")

            if "sourceRevision" not in seed:
                if name == "honua-server":
                    resolution = "AT-CUT"
                elif name in {"honua-console", "honua-sdk-dotnet", "honua-iac", "honua-helm"}:
                    resolution = "PUBLISH"
                else:
                    resolution = "MECHANICAL"
                refuse(f"{apath}.sourceRevision: registry provenance must bind the artifact to its source revision", resolution)

    # The matrix is consumed for contract coherence, but it does not manufacture missing versions.
    for contract, body in (matrix.get("contracts") or {}).items():
        expected = str((body or {}).get("version", ""))
        if expected and not any((c.get("contractVersions") or {}).get(contract) == expected for c in lock["components"].values()):
            unresolved.append(f"$.components: compatibility contract {contract!r} version {expected!r} has no component declaration")
    for message in [
        "$.contentDigests.geospatialMcp: certified content digest is not declared",
        "$.contentDigests.catalog: catalog digest is not declared",
        "$.contentDigests.okf: OKF digest is not declared",
        "$.fixtures: fixture repository revisions are not declared",
        "$.sbom: immutable SBOM references and hashes are not declared",
        "$.provenance: immutable provenance references and hashes are not declared",
        "$.notes: immutable release-notes content/reference is not declared",
    ]:
        refuse(message, "AT-CUT")
    return Draft(lock=lock, unresolved=unresolved, deferred_until_cut=deferred)


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
        if draft.deferred_until_cut:
            print(f"AT-CUT: {len(draft.deferred_until_cut)} deferred field(s) still block signing", file=sys.stderr)
        for item in draft.unresolved:
            print(f"- {item}", file=sys.stderr)
        return 1
    print(f"PASS: wrote complete draft {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
