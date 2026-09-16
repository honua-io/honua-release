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

from component_versions import version_map

from release_facts import (
    CONTENT_DIGEST_FACTS,
    content_digest,
    content_digest_conflicts,
    evidence_reference,
    fixture_reference,
    notes_reference,
)
from sdk_baselines import PUBLISHER, SDK_COMPONENTS, check_component, release_context

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]+(?:\.[0-9]+)?(?:-rc\.[0-9]+)?$")
PLACEHOLDER_RE = re.compile(r"(?:tbd|todo|unknown|unresolved|pending)", re.I)
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
    unresolved: list[str] = []
    deferred: list[str] = []

    def refuse(message: str, resolution: str) -> None:
        rendered = f"[{resolution}] {message}"
        unresolved.append(rendered)
        if resolution == "AT-CUT":
            deferred.append(rendered)
    if "disasterRecovery" in manifest:
        # Preserve the deployment-owned denominator in the signed lock. Never copy it from evidence.
        lock["disasterRecovery"] = manifest["disasterRecovery"]
    else:
        refuse("$.disasterRecovery: candidate deployment durable-substrate inventory is not declared", "AT-CUT")
    if not platform_id:
        unresolved.append(
            "$.platform.id: platformManifest.platformRelease is absent or not strict "
            "YYYY.N[.P][-rc.N]; refusing to infer the missing identity"
        )
    combined = list((manifest.get("components") or {}).items()) + list((manifest.get("experimental") or {}).items())
    # Join by package coordinate, not the manifest's arbitrary client row name.
    # A repository may publish several packages at different source revisions.
    published_by_component: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in combined}
    # A registry coordinate has exactly one owner across the whole platform. Scoping this to a
    # single component would let two rows claim the same coordinate for two repositories at
    # different versions/hashes, and every such row resolves to one owner on its own.
    coordinate_owner: dict[tuple[str, str], str] = {}
    for client, published in (manifest.get("clientArtifacts") or {}).items():
        path = f"$.clientArtifacts.{client}"
        if not isinstance(published, dict):
            refuse(f"{path}: published identity must be a mapping", "PUBLISH")
            continue
        kind = {"npm": "npm", "pypi": "wheel", "nuget": "nuget"}.get(published.get("ecosystem"))
        identity = {"kind": kind, "coordinate": published.get("package"),
                    "version": published.get("version"), "sourceRevision": published.get("sourceSha")}
        identity["integrity" if kind == "npm" else "sha256"] = published.get(
            "integrity" if kind == "npm" else "digest")
        if not all(identity.values()):
            refuse(f"{path}: incomplete published identity", "PUBLISH")
            continue
        owners = [name for name, component in combined
                  if _artifact_seed(component) == {"kind": kind, "coordinate": identity["coordinate"]}]
        repository = published.get("repository")
        if repository:
            repository = repository.removeprefix("https://github.com/")
            repository_owners = [name for name, component in combined
                                 if component.get("repository") == f"https://github.com/{repository}"]
            owners = [name for name in owners if name in repository_owners] if owners else repository_owners
        if len(owners) != 1:
            refuse(f"{path}: published package must resolve to exactly one component repository", "PUBLISH")
            continue
        name = owners[0]
        coordinate_key = (kind, identity["coordinate"])
        claimed = coordinate_owner.get(coordinate_key)
        if claimed is not None:
            owner = f" already owned by $.components.{claimed}" if claimed != name else ""
            refuse(f"{path}: duplicate published package coordinate{owner}", "PUBLISH")
            continue
        coordinate_owner[coordinate_key] = name
        published_by_component[name].append(identity)
    # Read the publisher's first-release facts from the manifest, not from a partially
    # built lock: component order must never decide whether a floor resolves.
    publisher_source = dict(combined).get(PUBLISHER) or {}
    release_ctx = release_context({"components": {PUBLISHER: publisher_source}})
    for name, component in combined:
        cpath = f"$.components.{name}"
        entry: dict[str, Any] = {
            "source": {"repository": component.get("repository"), "revision": component.get("sha")},
            "contractVersions": {},
            "schemaVersions": {},
            "artifacts": [],
            "artifactIdentityModel": "source-pinned" if component.get("sourcePinnedOnly") else "published",
        }
        for group in ("contractVersions", "schemaVersions"):
            if group in component:
                try:
                    entry[group] = version_map(component[group])
                except ValueError as exc:
                    refuse(f"{cpath}.{group}: {exc}", "MECHANICAL")
        if component.get("dbSchema") is not None:
            database = str(component["dbSchema"])
            declared_database = entry["schemaVersions"].get("database")
            if declared_database is not None and declared_database != database:
                refuse(f"{cpath}.schemaVersions.database: conflicts with dbSchema", "MECHANICAL")
            else:
                entry["schemaVersions"]["database"] = database
            if component.get("migrationJournalSha256"):
                entry["migrationJournalSha256"] = component["migrationJournalSha256"]
            else:
                refuse(f"{cpath}.migrationJournalSha256: exact declared migration set is not bound", "AT-CUT")
        seed = _artifact_seed(component)
        if seed:
            entry["artifacts"].append(seed)
        lock["components"][name] = entry
        if name == PUBLISHER:
            for field in ("releaseVersion", "publicationHistory"):
                if component.get(field):
                    entry[field] = component[field]
        if name in SDK_COMPONENTS:
            if component.get("serverCompatibility"):
                entry["serverCompatibility"] = component["serverCompatibility"]
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
        if not entry["contractVersions"]:
            resolution = "PUBLISH" if name in {"honua-sdk-dotnet", "honua-sdk-js", "honua-sdk-python"} else "AT-CUT"
            refuse(f"{cpath}.contractVersions: not declared", resolution)
        if not entry["schemaVersions"]:
            refuse(f"{cpath}.schemaVersions: not declared", "AT-CUT")
        if not seed and not component.get("sourcePinnedOnly") and not published_by_component[name]:
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
            published = next((item for item in published_by_component[name]
                              if item["kind"] == seed["kind"] and item["coordinate"] == seed["coordinate"]), {})
            conflicts = [key for key, value in published.items() if key in seed and seed[key] != value]
            component_hash = component.get("artifactSha256")
            if component_hash and published.get("sha256") and component_hash != published["sha256"]:
                conflicts.append("sha256")
            if conflicts:
                refuse(f"$.clientArtifacts.{name}: published identity conflicts with component artifact: "
                       + ", ".join(conflicts), "PUBLISH")
                published = {}
            if seed["kind"] == "npm":
                if published.get("integrity"):
                    seed["integrity"] = published["integrity"]
                    seed["sourceRevision"] = published["sourceRevision"]
                else:
                    refuse(f"{apath}.integrity: npm registry integrity is not declared", "MECHANICAL")
            elif seed["kind"] in ("nuget", "wheel", "terraform", "spec", "archive"):
                digest = component_hash or published.get("sha256")
                if digest and (component.get("artifactSourceRevision") or published.get("sourceRevision")):
                    seed["sha256"] = digest
                    seed["sourceRevision"] = component.get("artifactSourceRevision") or published.get("sourceRevision")
                else:
                    refuse(f"{apath}.sha256: package hash is not declared", "PUBLISH" if name == "honua-sdk-dotnet" else "MECHANICAL")
            elif seed["kind"] in ("image", "oci-chart"):
                digest = component.get("digest")
                if isinstance(digest, str) and digest.startswith("sha256:"):
                    seed["digest"] = digest
                else:
                    refuse(f"{apath}.digest: immutable registry digest is not declared", "PUBLISH")
                if component.get("architectures"):
                    seed["architectures"] = component["architectures"]
                else:
                    refuse(f"{apath}.architectures: registry architecture set is not declared", "AT-CUT" if name == "honua-server" else "PUBLISH")
                if seed["kind"] == "image":
                    platform_digests = component.get("platformDigests")
                    if isinstance(platform_digests, dict) and platform_digests:
                        seed["platformDigests"] = platform_digests
                    else:
                        refuse(f"{apath}.platformDigests: platform-specific image digests are not declared", "AT-CUT")
                else:
                    package_sha = component.get("artifactSha256")
                    if isinstance(package_sha, str) and package_sha.startswith("sha256:"):
                        seed["sha256"] = package_sha
                    else:
                        refuse(f"{apath}.sha256: pulled chart package checksum is not declared", "PUBLISH")

            if "sourceRevision" not in seed:
                if name == "honua-server":
                    resolution = "AT-CUT"
                elif name in {"honua-console", "honua-sdk-dotnet", "honua-iac", "honua-helm"}:
                    resolution = "PUBLISH"
                else:
                    resolution = "MECHANICAL"
                refuse(f"{apath}.sourceRevision: registry provenance must bind the artifact to its source revision", resolution)

        for published in published_by_component[name]:
            if not seed or (published["kind"], published["coordinate"]) != (seed["kind"], seed["coordinate"]):
                entry["artifacts"].append(published)
        if name in SDK_COMPONENTS:
            try:
                check_component(entry, release_ctx)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                refuse(f"{cpath}.serverCompatibility: {exc}", "PUBLISH")

        for client, blocker in (component.get("pendingPublishedClients") or {}).items():
            refuse(f"{cpath}.artifacts[{client}]: published package coordinate is pending {blocker}", "PUBLISH")

    # The matrix is consumed for contract coherence, but it does not manufacture missing versions.
    for contract, body in (matrix.get("contracts") or {}).items():
        expected = str((body or {}).get("version", ""))
        if expected and not any(
            (component.get("contractVersions") or {}).get(contract) == expected
            for component in lock["components"].values()
        ):
            unresolved.append(f"$.components: compatibility contract {contract!r} version {expected!r} has no component declaration")
    _release_facts(manifest, lock, refuse)
    return Draft(lock=lock, unresolved=unresolved, deferred_until_cut=deferred)


def _release_facts(manifest: dict[str, Any], lock: dict[str, Any], refuse: Any) -> None:
    """Consume the declared release-level facts; refuse every fact that is absent or mutable.

    These are the parts of the candidate identity that no component owns. They are declared by
    `platformLockEvidence` in the frozen platform manifest so that a reviewer, the generator and
    candidate binding all read the same bytes; nothing here is inferred from the release label.
    """
    evidence = manifest.get("platformLockEvidence")
    if evidence is not None and not isinstance(evidence, dict):
        refuse("$.platformLockEvidence: release-level declarations must be a mapping", "AT-CUT")
        evidence = {}
    evidence = evidence or {}

    declared_digests = evidence.get("contentDigests") or {}
    if not isinstance(declared_digests, dict):
        refuse("$.contentDigests: declarations must be a mapping of content digests", "AT-CUT")
        declared_digests = {}
    for name, description in CONTENT_DIGEST_FACTS:
        if name not in declared_digests:
            refuse(f"$.contentDigests.{name}: {description} is not declared", "AT-CUT")
            continue
        try:
            digest, _ = content_digest(declared_digests[name])
        except (ValueError, TypeError) as exc:
            refuse(f"$.contentDigests.{name}: {exc}", "AT-CUT")
            continue
        lock["contentDigests"][name] = digest
    for name in sorted(set(declared_digests) - {key for key, _ in CONTENT_DIGEST_FACTS}):
        refuse(f"$.contentDigests.{name}: the lock schema declares no such content digest", "AT-CUT")
    # One standard, one identity: the lock copies component artifacts and content digests from
    # independent manifest fields, so a disagreement would sign two identities for the same bytes.
    for name, message in content_digest_conflicts(manifest):
        refuse(message, "MECHANICAL")
        lock["contentDigests"].pop(name, None)

    declared_fixtures = evidence.get("fixtures") or []
    if not isinstance(declared_fixtures, list):
        refuse("$.fixtures: fixture declarations must be a list", "AT-CUT")
        declared_fixtures = []
    seen: set[tuple[str, str]] = set()
    for index, declaration in enumerate(declared_fixtures):
        try:
            reference = fixture_reference(declaration)
        except (ValueError, TypeError) as exc:
            refuse(f"$.fixtures[{index}]: {exc}", "AT-CUT")
            continue
        key = (reference["repository"], reference.get("path", ""))
        if key in seen:
            refuse(f"$.fixtures[{index}]: duplicate fixture source declaration", "AT-CUT")
            continue
        seen.add(key)
        lock["fixtures"].append(reference)
    if not lock["fixtures"]:
        refuse("$.fixtures: fixture repository revisions are not declared", "AT-CUT")

    # Mechanical binding: every reference names a locked component, and every component whose
    # artifacts the candidate publishes is covered. This is what can be checked from the frozen
    # inputs alone; it does not assert that the referenced document describes those exact bytes.
    published = {name for name, entry in lock["components"].items() if entry["artifacts"]}
    for field, description in (("sbom", "SBOM"), ("provenance", "provenance")):
        declarations = evidence.get(field) or []
        if not isinstance(declarations, list):
            refuse(f"$.{field}: {description} declarations must be a list", "AT-CUT")
            declarations = []
        for index, declaration in enumerate(declarations):
            try:
                reference = evidence_reference(declaration)
            except (ValueError, TypeError) as exc:
                refuse(f"$.{field}[{index}]: {exc}", "AT-CUT")
                continue
            if reference["component"] not in lock["components"]:
                refuse(f"$.{field}[{index}]: names {reference['component']!r}, which is not a "
                       "component of this candidate", "MECHANICAL")
                continue
            lock[field].append(reference)
        if not lock[field]:
            refuse(f"$.{field}: immutable {description} references and hashes are not declared", "AT-CUT")
            continue
        uncovered = sorted(published - {reference["component"] for reference in lock[field]})
        if uncovered:
            refuse(f"$.{field}: no {description} reference covers the candidate artifacts of "
                   + ", ".join(uncovered), "AT-CUT")

    declared_notes = evidence.get("notes")
    if declared_notes is None:
        artifacts = manifest.get("artifacts")
        declared_notes = artifacts.get("releaseNotes") if isinstance(artifacts, dict) else None
    if isinstance(declared_notes, str) and PLACEHOLDER_RE.fullmatch(declared_notes.strip()):
        declared_notes = None  # a placeholder is an absent declaration, never a reference
    if declared_notes is None:
        refuse("$.notes: immutable release-notes content/reference is not declared", "AT-CUT")
        return
    try:
        lock["notes"] = notes_reference(declared_notes)
    except (ValueError, TypeError) as exc:
        refuse(f"$.notes: {exc}", "AT-CUT")


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
