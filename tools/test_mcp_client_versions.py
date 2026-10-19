"""Selected MCP producer dependencies govern versions without changing coverage."""
import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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


@pytest.mark.parametrize("case", ["unchanged", "package.json", "package-lock.json", "consistent-version-change", "fetch-failure", "invalid-pin"])
def test_required_workflow_authenticates_dependency_snapshots(tmp_path, case):
    """Execute the required shell step; internally coherent edits must still fail."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/manifest-validate.yml").read_text())
    step = next(step for step in workflow["jobs"]["validate"]["steps"]
                if step.get("name") == "Authenticate selected MCP client dependency snapshots")
    selected = tmp_path / "certification/sources"
    shutil.copytree(ROOT / "certification/sources/geospatial-mcp", selected / "geospatial-mcp")
    shutil.copy(ROOT / "certification/sources/source-revisions.v1.json", selected / "source-revisions.v1.json")
    upstream = tmp_path / "upstream"
    shutil.copytree(selected / "geospatial-mcp", upstream)
    if case in ("package.json", "package-lock.json"):
        # Even a parse-equivalent byte change must fail source authentication.
        with (selected / "geospatial-mcp" / case).open("a") as stream:
            stream.write("\n")
    elif case == "consistent-version-change":
        for filename in ("package.json", "package-lock.json"):
            path = selected / "geospatial-mcp" / filename
            path.write_text(path.read_text().replace("2.3.0", "2.10.1"))
    elif case == "invalid-pin":
        path = selected / "source-revisions.v1.json"
        data = json.loads(path.read_text())
        data["sources"]["geospatial-mcp"]["commit"] = "trunk"
        path.write_text(json.dumps(data))
    shim = tmp_path / "bin"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
pin = json.loads(pathlib.Path(os.environ['MCP_REAL_REVISIONS']).read_text())['sources']['geospatial-mcp']['commit']
urls = [arg for arg in args if arg.startswith('https://')]
names = ['package.json', 'package-lock.json']
name = next((name for name in names if urls == [f'https://api.github.com/repos/honua-io/geospatial-mcp/contents/certification/{name}?ref={pin}']), None)
if name is None or os.environ['MCP_CASE'] == 'fetch-failure':
    sys.exit(22)
with pathlib.Path(os.environ['MCP_FETCH_LOG']).open('a') as stream:
    stream.write(name + '\\n')
pathlib.Path(args[args.index('-o') + 1]).write_bytes((pathlib.Path(os.environ['MCP_UPSTREAM']) / name).read_bytes())
""")
    curl.chmod(0o755)
    fetch_log = tmp_path / "fetch.log"
    result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
                                 "RUNNER_TEMP": str(tmp_path), "GH_TOKEN": "test-token",
                                 "MCP_REAL_REVISIONS": str(ROOT / "certification/sources/source-revisions.v1.json"),
                                 "MCP_UPSTREAM": str(upstream), "MCP_CASE": case, "MCP_FETCH_LOG": str(fetch_log)},
                            timeout=15)
    if case == "unchanged":
        assert result.returncode == 0, result.stderr
        assert fetch_log.read_text().splitlines() == ["package.json", "package-lock.json"]
    else:
        assert result.returncode != 0, "required source authentication accepted tampering or missing proof"
