"""Selected MCP producer dependencies govern versions without changing coverage."""
import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mcp_requirements_generator", ROOT / "certification/generate-protocol-requirements.py")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
SDK = "@modelcontextprotocol/sdk"
INSPECTOR = "@modelcontextprotocol/inspector"


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    shutil.copytree(ROOT / "certification/sources", source)
    monkeypatch.setattr(GENERATOR, "SOURCES", source)
    monkeypatch.setattr(GENERATOR, "OUTPUT", tmp_path / "catalog.json")
    directory = source / "geospatial-mcp"
    package = json.loads((directory / "package.json").read_text())
    lock = json.loads((directory / "package-lock.json").read_text())
    return directory, package, lock


def write(snapshots):
    directory, package, lock = snapshots
    (directory / "package.json").write_text(json.dumps(package))
    (directory / "package-lock.json").write_text(json.dumps(lock))


def test_frozen_mcp_versions_reproduce_entire_catalog(snapshots):
    GENERATOR.main()
    assert GENERATOR.OUTPUT.read_bytes() == (ROOT / "certification/protocol-certification-requirements.v1.json").read_bytes()


def test_selected_versions_change_only_mcp_versions_preserving_every_required_row(snapshots):
    GENERATOR.main()
    before = json.loads(GENERATOR.OUTPUT.read_text())
    _, package, lock = snapshots
    for name, version in ((SDK, "1.32.1"), (INSPECTOR, "2.10.1")):
        package["dependencies"][name] = version
        lock["packages"][""]["dependencies"][name] = version
        entry = lock["packages"]["node_modules/" + name]
        entry["version"] = version
        entry["resolved"] = f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[1]}-{version}.tgz"
    # Controlled future selection; these modified lock bytes are not published evidence.
    write(snapshots)
    GENERATOR.main()
    after = json.loads(GENERATOR.OUTPUT.read_text())
    expected = copy.deepcopy(before)
    counts = {"mcp-typescript-sdk": 0, "mcp-inspector": 0}
    for row in expected["requirements"]:
        if row["client_lane"] in counts:
            counts[row["client_lane"]] += 1
            row["client_version"] = "1.32.1" if row["client_lane"] == "mcp-typescript-sdk" else "2.10.1"
    assert all(counts.values()) and counts["mcp-typescript-sdk"] == counts["mcp-inspector"]
    assert after == expected  # No dropped facets, operations, producers, tiers or source pins.


@pytest.mark.parametrize("mutation", ["range", "missing", "root-version", "installed-version", "registry", "integrity", "lock-version", "package-name", "root-name"])
def test_invalid_selected_dependency_refuses_generation(snapshots, mutation):
    _, package, lock = snapshots
    entry = lock["packages"]["node_modules/" + INSPECTOR]
    if mutation == "range": package["dependencies"][INSPECTOR] = "^2.3.0"
    if mutation == "missing": del package["dependencies"][INSPECTOR]
    if mutation == "root-version": lock["packages"][""]["dependencies"][INSPECTOR] = "2.10.1"
    if mutation == "installed-version": entry["version"] = "2.10.1"
    if mutation == "registry": entry["resolved"] = "https://other.example/inspector-2.3.0.tgz"
    if mutation == "integrity": entry["integrity"] = "sha512-invalid"
    if mutation == "lock-version": lock["lockfileVersion"] = 2
    if mutation == "package-name": package["name"] = "other-package"
    if mutation == "root-name": lock["packages"][""]["name"] = "other-package"
    write(snapshots)
    with pytest.raises(ValueError, match="pinned MCP"):
        GENERATOR.main()
    assert not GENERATOR.OUTPUT.exists()


def test_convergence_fetches_both_literal_files_at_selected_mcp_sha(snapshots):
    from convergence_rebind import fetch_snapshots, VENDORED
    directory, _, _ = snapshots
    revisions = json.loads((ROOT / "certification/sources/source-revisions.v1.json").read_text())["sources"]
    pins = {name: item["commit"] for name, item in revisions.items()}
    pins["geospatial-mcp"] = "a" * 40
    calls = []

    class Fetcher:
        def content(self, repository, path, revision):
            calls.append((repository, path, revision))
            if repository == "honua-io/geospatial-mcp":
                return (directory / Path(path).name).read_bytes()
            local = next(local for source, mappings in VENDORED.items()
                         for upstream, local in mappings
                         if revisions[source]["repository"] == repository and upstream == path)
            return (ROOT / local).read_bytes()

    payloads = fetch_snapshots(ROOT, Fetcher(), revisions, pins)
    assert [call for call in calls if call[0] == "honua-io/geospatial-mcp"] == [
        ("honua-io/geospatial-mcp", "certification/package.json", "a" * 40),
        ("honua-io/geospatial-mcp", "certification/package-lock.json", "a" * 40),
    ]
    for name in ("package.json", "package-lock.json"):
        assert payloads[f"certification/sources/geospatial-mcp/{name}"] == (directory / name).read_bytes()
