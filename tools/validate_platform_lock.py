#!/usr/bin/env python3
"""Fail-closed semantic validator for platform-lock.v1 YAML/JSON documents."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover
    raise SystemExit("jsonschema is required: pip install jsonschema") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_SCHEMA = REPO_ROOT / "schemas" / "platform-lock.v1.schema.json"

PLACEHOLDER_RE = re.compile(r"(?:^|[-_ ])(?:tbd|todo|unknown|unresolved)(?:$|[-_ :])", re.I)
CARRIED_FORWARD_RE = re.compile(r"carried[ -]?forward|carry[ -]?forward", re.I)
FLOATING_TAGS = {"latest", "nightly", "nightly-aot", "edge", "dev", "main", "master", "trunk", "stable"}
EXACT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")


def load_lock(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _require(mapping: dict, keys: tuple[str, ...], path: str, f: Findings) -> None:
    for key in keys:
        if key not in mapping:
            f.error(f"{path}.{key}", "required field is missing")


def validate(lock: dict[str, Any]) -> Findings:
    f = Findings()
    schema = json.loads(LOCK_SCHEMA.read_text(encoding="utf-8"))
    for error in sorted(Draft202012Validator(schema).iter_errors(lock), key=lambda item: list(item.absolute_path)):
        path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path)
        f.error(path, f"schema violation: {error.message}")
    _require(lock, ("lockVersion", "platform", "sourceInputs", "components", "contentDigests", "fixtures", "sbom", "provenance", "notes"), "$", f)
    if lock.get("lockVersion") != "platform-lock.v1":
        f.error("$.lockVersion", "must equal 'platform-lock.v1'")
    platform = lock.get("platform") or {}
    if isinstance(platform, dict) and platform.get("supportTier") != "ga":
        f.error("$.platform.supportTier", "must equal the operator-locked platform tier 'ga'")

    for path, value in _walk(lock):
        if isinstance(value, str):
            if PLACEHOLDER_RE.search(value):
                f.error(path, "placeholder/TBD values are forbidden")
            if CARRIED_FORWARD_RE.search(value):
                f.error(path, "carried-forward markers are forbidden")

    components = lock.get("components")
    if not isinstance(components, dict) or not components:
        f.error("$.components", "must be a non-empty mapping")
        return f

    for name, component in components.items():
        path = f"$.components.{name}"
        if not isinstance(component, dict):
            f.error(path, "must be a mapping")
            continue
        _require(component, ("source", "lifecycleStatus", "supportTier", "artifactIdentityModel", "contractVersions", "schemaVersions", "artifacts"), path, f)
        lifecycle_status = component.get("lifecycleStatus")
        if component.get("supportTier") != str(lifecycle_status).lower():
            f.error(f"{path}.supportTier", "must be derived from lifecycleStatus by lowercasing it")
        source = component.get("source") or {}
        revision = source.get("revision") if isinstance(source, dict) else None
        if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
            f.error(f"{path}.source.revision", "must be an immutable 40-character git revision")
        artifacts = component.get("artifacts")
        if component.get("artifactIdentityModel") == "source-pinned" and artifacts == []:
            continue
        if not isinstance(artifacts, list) or not artifacts:
            f.error(f"{path}.artifacts", "must contain at least one released artifact")
            continue
        for index, artifact in enumerate(artifacts):
            apath = f"{path}.artifacts[{index}]"
            if not isinstance(artifact, dict):
                f.error(apath, "must be a mapping")
                continue
            _require(artifact, ("kind", "coordinate", "version", "sourceRevision"), apath, f)
            artifact_revision = artifact.get("sourceRevision")
            if revision and artifact_revision and revision != artifact_revision:
                f.error(f"{apath}.sourceRevision", f"artifact identity revision {artifact_revision!r} conflicts with component source revision {revision!r}")
            version = str(artifact.get("version", ""))
            if not EXACT_VERSION_RE.fullmatch(version):
                f.error(f"{apath}.version", "must be an exact released SemVer (ranges, tags, and source-built identities are forbidden)")
            coordinate = str(artifact.get("coordinate", ""))
            tag = coordinate.rsplit(":", 1)[-1].lower() if ":" in coordinate else ""
            if tag in FLOATING_TAGS or any(coordinate.lower().endswith(f":{item}") for item in FLOATING_TAGS):
                f.error(f"{apath}.coordinate", f"floating tag {tag!r} is forbidden")
            kind = artifact.get("kind")
            if kind == "npm" and not str(artifact.get("integrity", "")).startswith("sha512-"):
                f.error(f"{apath}.integrity", "npm artifacts require an sha512 integrity value")
            if kind in ("nuget", "wheel") and not DIGEST_RE.fullmatch(str(artifact.get("sha256", ""))):
                f.error(f"{apath}.sha256", f"{kind} artifacts require a sha256 hash")
            if kind in ("spec", "archive") and not DIGEST_RE.fullmatch(str(artifact.get("sha256", ""))):
                f.error(f"{apath}.sha256", f"{kind} artifacts require a sha256 hash")
            if kind in ("image", "oci-chart"):
                if not DIGEST_RE.fullmatch(str(artifact.get("digest", ""))):
                    f.error(f"{apath}.digest", f"{kind} artifacts require an immutable sha256 digest")
                if not artifact.get("architectures"):
                    f.error(f"{apath}.architectures", f"{kind} artifacts require an architecture set")
            if kind == "image":
                architectures = artifact.get("architectures")
                architecture_digests = artifact.get("architectureDigests")
                fargate_architectures = artifact.get("awsFargateArchitectures")
                if isinstance(architectures, list):
                    published = set(architectures)
                    for field, values in (
                        ("architectureDigests", architecture_digests),
                        ("awsFargateArchitectures", fargate_architectures),
                    ):
                        if not isinstance(values, dict):
                            continue
                        for architecture in sorted(published - set(values)):
                            f.error(
                                f"{apath}.{field}.{architecture}",
                                "published architecture requires an explicit entry",
                            )
                        for architecture in sorted(set(values) - published):
                            f.error(
                                f"{apath}.{field}.{architecture}",
                                "entry refers to an unpublished architecture",
                            )
    return f


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    args = parser.parse_args(argv)
    findings = validate(load_lock(args.lock))
    if findings.ok:
        print(f"PASS: {args.lock} is a valid platform-lock.v1")
        return 0
    print(f"REFUSED: {len(findings.errors)} platform lock violation(s)", file=sys.stderr)
    for error in findings.errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
