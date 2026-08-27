import importlib.util
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("convergence_rebind", ROOT / "tools/convergence_rebind.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class StubGitHub:
    def __init__(self, root):
        self.root = root

    def head(self, repository):
        return {"honua-io/honua-esri-compat": "1" * 40, "cloudnativegeo/cloud-optimized-geospatial-formats-guide": "2" * 40}[repository]

    def content(self, repository, path, revision):
        local = next(local for source, mappings in MODULE.VENDORED.items() for upstream, local in mappings if upstream == path and repository.endswith(MODULE.load_json(self.root, MODULE.REVISIONS)["sources"][source]["repository"].split("/")[-1]))
        return (self.root / local).read_bytes()


def fixture(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "rebind-plan.json"))
    return root


def test_plan_golden_uses_frozen_pins_without_network(tmp_path):
    root = fixture(tmp_path)
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    pins = {row["source"]: (row["target"][:7], row["rule"]) for row in plan["sources"]}
    # Golden values track platform-manifest.yaml; refreshed with the 2026-08-27 working snapshot.
    assert pins["sdk-dotnet"] == ("8e4dd3d", "manifest/frozen")
    assert pins["sdk-python"] == ("516c727", "manifest/frozen")
    assert pins["sdk-js"] == ("9f859b2", "manifest/frozen")
    assert pins["server-certification"] == ("ac30266", "manifest/frozen")
    assert plan["receipt_schema_min"] == {"current": "v1", "proposed": "v1"}


def test_apply_changes_only_planned_repository_files(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    plan, payloads, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "", "returncode": 0})())
    # Assert the complete intended payload rather than mutating the real worktree.
    assert set(payloads) == {str(MODULE.REVISIONS), str(MODULE.CATALOG), "certification/generate-protocol-requirements.py", *(p for mappings in MODULE.VENDORED.values() for _, p in mappings)}
    assert tuple(MODULE.CALLERS) == (Path(".github/workflows/pr-protocol-certification.yml"), Path(".github/workflows/nightly-protocol-certification.yml"), Path(".github/workflows/release-train.yml"))


def test_post_merge_block_names_each_variable_exactly_once():
    block = MODULE.post_merge_commands()
    for name in ("PROTOCOL_CERTIFICATION_MATRIX_COMMIT", "PROTOCOL_CERTIFICATION_MATRIX_SHA256", "PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION"):
        assert block.count(name) == 1
