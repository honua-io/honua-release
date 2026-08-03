"""Load the platform manifest + compatibility matrix — the source of truth for what versions ship
together. The runner pins the composed server image and the SDK versions from here so a scenario run is
the executable form of one compatibility-matrix row.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "PyYAML is required: pip install -r e2e/requirements.txt"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"
MATRIX_PATH = REPO_ROOT / "compatibility-matrix.yaml"

# A pin is a placeholder (not a real, runnable artifact) when it is unset or still carries the
# scaffold sentinel. Server-dependent scenarios are BLOCKED on a placeholder pin.
PLACEHOLDERS = {"TBD", "", None}


def _is_placeholder(value: str | None) -> bool:
    if value in PLACEHOLDERS:
        return True
    return str(value).strip().upper().endswith(":TBD") or str(value).strip() == "TBD"


@dataclass
class ServerPin:
    version: str
    image: str

    @property
    def is_real(self) -> bool:
        """True only when the manifest points at a concrete, pullable image."""
        return not (_is_placeholder(self.version) or _is_placeholder(self.image))


@dataclass
class SdkPin:
    name: str
    version: str
    artifact: str  # e.g. "npm:@honua/sdk-js"

    @property
    def coord(self) -> str:
        """Package coordinate without the registry-kind prefix (e.g. '@honua/sdk-js')."""
        return self.artifact.split(":", 1)[1] if ":" in self.artifact else self.artifact

    @property
    def is_real(self) -> bool:
        return not _is_placeholder(self.version)


@dataclass
class Manifest:
    platform_release: str
    server: ServerPin
    sdks: dict[str, SdkPin]
    raw: dict

    @property
    def server_image(self) -> str:
        # Allow an explicit override (locally-built/staging image) but default to the manifest pin so
        # the manifest stays the source of truth.
        return os.environ.get("HONUA_SERVER_IMAGE") or self.server.image


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        # The scaffold manifest still carries non-YAML placeholders (e.g. contractVersions `v?`) until
        # Phase 0 encodes real values. Degrade to all-placeholder pins so the harness stays runnable
        # and reports BLOCKED honestly (rather than crashing / fabricating a green). TODO(#7): once the
        # manifest holds real, valid pins this branch is dead.
        print(f"WARN: platform-manifest.yaml not yet valid YAML ({e}); treating all pins as placeholder")
        data = {}
    components = data.get("components", {}) or {}

    srv = components.get("honua-server", {}) or {}
    server = ServerPin(version=str(srv.get("version", "TBD")), image=str(srv.get("image", "TBD")))

    sdk_keys = {
        "js": "honua-sdk-js",
        "python": "honua-sdk-python",
        "dotnet": "honua-sdk-dotnet",
    }
    sdks: dict[str, SdkPin] = {}
    for short, key in sdk_keys.items():
        comp = components.get(key, {}) or {}
        sdks[short] = SdkPin(
            name=key,
            version=str(comp.get("version", "TBD")),
            artifact=str(comp.get("artifact", "")),
        )

    return Manifest(
        platform_release=str(data.get("platformRelease", "unknown")),
        server=server,
        sdks=sdks,
        raw=data,
    )
