"""JS catalog identity follows the immutable source declarations (release#181)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    shutil.copytree(ROOT / "certification", tmp_path / "certification", ignore=shutil.ignore_patterns(
        "__pycache__", "installed-clients", "terminal-journey", "first-publication", "release-controls",
    ))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    return tmp_path


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def generate(checkout):
    return subprocess.run([sys.executable, str(checkout / "certification/generate-protocol-requirements.py")],
                          cwd=checkout, capture_output=True, text=True, timeout=60)


def declarations(checkout, version):
    coverage = checkout / "certification/sources/sdk-js/sdk-coverage.v1.json"
    contract = checkout / "certification/sources/sdk-js/protocol-certification.v1.json"
    data = read(coverage)
    data["sdk"]["version"] = version
    write(coverage, data)
    data = read(contract)
    data["clientVersion"] = version
    write(contract, data)
    return coverage, contract


def test_current_pinned_catalog_regenerates_byte_identically(checkout):
    target = checkout / "certification/protocol-certification-requirements.v1.json"
    original = target.read_bytes()
    result = generate(checkout)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == original


def test_new_pinned_js_version_changes_all_js_rows_only(checkout):
    target = checkout / "certification/protocol-certification-requirements.v1.json"
    before = read(target)
    declarations(checkout, "0.2.0")
    result = generate(checkout)
    assert result.returncode == 0, result.stderr
    after = read(target)
    old_rows, new_rows = before["requirements"], after["requirements"]
    assert len(old_rows) == len(new_rows)
    js_rows = [row for row in new_rows if row["canonical_client"] == "@honua/sdk-js"]
    assert js_rows and {row["client_version"] for row in js_rows} == {"0.2.0"}
    assert {row["client_lane"] for row in js_rows} >= {"sdk-js", "sdk-js-certification"}
    for old, new in zip(old_rows, new_rows):
        if old["canonical_client"] == "@honua/sdk-js":
            assert {k: v for k, v in new.items() if k not in ("id", "client_version")} == {
                k: v for k, v in old.items() if k not in ("id", "client_version")}
        else:
            assert new == old
    assert after["source_revisions"] == before["source_revisions"]


@pytest.mark.parametrize("version", [None, "", "latest", "0.2", "01.2.0", "0.2.0-01", "0.2.0\n"])
def test_invalid_pinned_version_refused_before_rewriting_catalog(checkout, version):
    target = checkout / "certification/protocol-certification-requirements.v1.json"
    original = target.read_bytes()
    declarations(checkout, version)
    result = generate(checkout)
    assert result.returncode != 0
    assert "exact SemVer" in result.stderr
    assert target.read_bytes() == original


@pytest.mark.parametrize("field,value", [("clientVersion", "0.2.1"), ("canonicalClient", "@other/sdk")])
def test_inconsistent_source_declarations_refused(checkout, field, value):
    _, contract = declarations(checkout, "0.2.0")
    data = read(contract)
    data[field] = value
    write(contract, data)
    result = generate(checkout)
    assert result.returncode != 0
    assert "same client and version" in result.stderr


def test_wrong_coverage_package_refused(checkout):
    coverage, _ = declarations(checkout, "0.2.0")
    data = read(coverage)
    data["sdk"]["package"] = "@other/sdk"
    write(coverage, data)
    result = generate(checkout)
    assert result.returncode != 0
    assert "@honua/sdk-js" in result.stderr
