"""Exercise the real denominator generator and exact-source snapshot fetch."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path("certification/protocol-certification-requirements.v1.json")
DECLARATIONS = {
    "sdk-python": ("protocol-certification.v1.json", "clientVersion", "Honua SDK Python", "0.1.15"),
    "sdk-dotnet": ("release-please-manifest.json", ".", "Honua SDK .NET", "1.11.0"),
}


@pytest.fixture
def checkout(tmp_path):
    shutil.copytree(ROOT / "certification", tmp_path / "certification", ignore=shutil.ignore_patterns(
        "__pycache__", "installed-clients", "terminal-journey", "first-publication", "release-controls",
    ))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    return tmp_path


def generate(checkout):
    return subprocess.run([sys.executable, str(checkout / "certification/generate-protocol-requirements.py")],
                          cwd=checkout, capture_output=True, text=True, timeout=60)


def change_declaration(checkout, source, version):
    filename, key, _, _ = DECLARATIONS[source]
    path = checkout / "certification/sources" / source / filename
    declaration = json.loads(path.read_text())
    declaration[key] = version
    path.write_text(json.dumps(declaration) + "\n")


def test_existing_source_versions_leave_historical_catalog_byte_identical(checkout):
    original = (checkout / CATALOG).read_bytes()
    result = generate(checkout)
    assert result.returncode == 0, result.stderr
    assert (checkout / CATALOG).read_bytes() == original


@pytest.mark.parametrize("source", DECLARATIONS)
def test_new_source_version_updates_both_lanes_without_changing_other_rows(checkout, source):
    _, _, client, version = DECLARATIONS[source]
    before = json.loads((checkout / CATALOG).read_text())
    change_declaration(checkout, source, version)
    result = generate(checkout)
    assert result.returncode == 0, result.stderr
    after = json.loads((checkout / CATALOG).read_text())
    old_rows, new_rows = before["requirements"], after["requirements"]
    assert len(old_rows) == len(new_rows)
    lanes = {source, source + "-certification"}
    affected = [row for row in new_rows if row["canonical_client"] == client and row["client_lane"] in lanes]
    assert {row["client_lane"] for row in affected} == lanes
    assert {row["client_version"] for row in affected} == {version}
    for old, new in zip(old_rows, new_rows):
        if old["canonical_client"] == client and old["client_lane"] in lanes:
            assert {key: value for key, value in old.items() if key not in {"id", "client_version"}} == {
                key: value for key, value in new.items() if key not in {"id", "client_version"}}
        else:
            assert old == new
    assert before["source_revisions"] == after["source_revisions"]


@pytest.mark.parametrize("source", DECLARATIONS)
@pytest.mark.parametrize("version", [None, "", "latest", "1.11", "01.11.0", "1.11.0-01", "1.11.0\n"])
def test_invalid_source_version_refuses_without_rewriting_catalog(checkout, source, version):
    original = (checkout / CATALOG).read_bytes()
    change_declaration(checkout, source, version)
    result = generate(checkout)
    assert result.returncode != 0
    assert "exact SemVer" in result.stderr
    assert (checkout / CATALOG).read_bytes() == original


@pytest.mark.parametrize("field", ["canonicalClient", "sourceRepository"])
def test_wrong_python_client_identity_refuses(checkout, field):
    path = checkout / "certification/sources/sdk-python/protocol-certification.v1.json"
    declaration = json.loads(path.read_text())
    declaration[field] = "another-client"
    path.write_text(json.dumps(declaration))
    original = (checkout / CATALOG).read_bytes()
    result = generate(checkout)
    assert result.returncode != 0
    assert "unexpected client or repository" in result.stderr
    assert (checkout / CATALOG).read_bytes() == original


def test_rebind_fetches_version_bytes_at_the_same_dotnet_source_pin():
    spec = importlib.util.spec_from_file_location("source_version_rebind", ROOT / "tools/convergence_rebind.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = {"sdk-dotnet": {"repository": "honua-io/honua-sdk-dotnet"}}
    pin = "5f09e4fc" + "a" * 32
    calls = []
    version_bytes = b'{".": "1.11.0"}\n'

    class GitHub:
        def content(self, repository, path, revision):
            calls.append((repository, path, revision))
            return version_bytes if path == ".release-please-manifest.json" else b"{}"

    module.VENDORED = {"sdk-dotnet": module.VENDORED["sdk-dotnet"]}
    snapshots = module.fetch_snapshots(ROOT, GitHub(), source, {"sdk-dotnet": pin})
    assert snapshots["certification/sources/sdk-dotnet/release-please-manifest.json"] == version_bytes
    assert ("honua-io/honua-sdk-dotnet", ".release-please-manifest.json", pin) in calls
    assert all(repository == "honua-io/honua-sdk-dotnet" and revision == pin
               for repository, _, revision in calls)
