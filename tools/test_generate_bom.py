"""Tests for the platform BOM generator.

A BOM the customer scans must be valid CycloneDX, cover every shipped component, carry correct purls,
and be deterministic (so it is reproducible + diff-able). All checked against the real manifest.

Run: python -m pytest tools/test_generate_bom.py    (or: python tools/test_generate_bom.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_bom as gb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TS = "2026-07-01T00:00:00Z"


def _real_manifest():
    import yaml
    return yaml.safe_load((REPO_ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))


def test_bom_is_valid_cyclonedx_shell():
    bom = gb.build_bom(_real_manifest(), "2026.1", TS)
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.5"
    assert bom["serialNumber"].startswith("urn:uuid:")
    assert bom["metadata"]["component"]["name"] == "honua-platform"
    assert bom["metadata"]["component"]["version"] == "2026.1"
    assert bom["metadata"]["timestamp"] == TS


def test_bom_covers_every_component():
    manifest = _real_manifest()
    bom = gb.build_bom(manifest, "2026.1", TS)
    names = {c["name"] for c in bom["components"]}
    assert names == set(manifest["components"]), f"missing: {set(manifest['components']) - names}"


def test_label_rc_is_stripped():
    bom = gb.build_bom(_real_manifest(), "2026.1-rc.4", TS)
    assert bom["metadata"]["component"]["version"] == "2026.1"


def test_purl_for_npm_scoped_and_pypi():
    assert gb._purl("npm:@honua-io/sdk-js", "0.0.14-alpha.0") == "pkg:npm/%40honua-io/sdk-js@0.0.14-alpha.0"
    assert gb._purl("pypi:honua-sdk", "0.1.4") == "pkg:pypi/honua-sdk@0.1.4"
    assert gb._purl("nuget:Honua.Sdk", "1.3.0") == "pkg:nuget/Honua.Sdk@1.3.0"
    assert gb._purl("", "1.0.0") is None


def test_pre_release_version_has_purl_without_version_suffix():
    # A library pinned at "pre-release" still gets a purl (name only) — version omitted, not faked.
    assert gb._purl("npm:@honua-io/sdk-js", "pre-release") == "pkg:npm/%40honua-io/sdk-js"


def test_container_component_carries_image_and_vcs_properties():
    manifest = {"components": {"honua-server": {
        "version": "pre-release", "sha": "a" * 40,
        "image": "ghcr.io/honua-io/honua-server:nightly-aot",
        "contractVersions": {"admin": "v1"}, "dbSchema": "metadata-v1"}}}
    entry = gb.build_bom(manifest, "2026.1", TS)["components"][0]
    assert entry["type"] == "container"
    props = {p["name"]: p["value"] for p in entry["properties"]}
    assert props["honua:image"].endswith("nightly-aot")
    assert props["honua:vcs"].endswith("a" * 40)
    assert props["honua:contract:admin"] == "v1"
    assert props["honua:dbSchema"] == "metadata-v1"


def test_bom_is_deterministic():
    m = _real_manifest()
    assert gb.build_bom(m, "2026.1", TS) == gb.build_bom(m, "2026.1", TS)
    # Same component set -> same serial number regardless of label suffix.
    assert gb.build_bom(m, "2026.1-rc.1", TS)["serialNumber"] == gb.build_bom(m, "2026.1", TS)["serialNumber"]


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
