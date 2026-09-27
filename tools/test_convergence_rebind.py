import importlib.util
import json
import shutil

import pytest
import yaml
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


def align_components_to_published(root: Path) -> None:
    """Point component SHAs at the published sourceShas without touching the ledger."""
    path = root / MODULE.MANIFEST
    text = path.read_text(encoding="utf-8")
    manifest = yaml.safe_load(text)
    for _source, component, artifact in MODULE.SDK_PRODUCERS:
        current = manifest["components"][component]["sha"]
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        if current != published:
            text = MODULE.replace_scalar(text, "sha", current, published)
    path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["protocolCertification"]["ledger"]["status"] == "pending"


def test_plan_refuses_sdk_component_pins_that_are_not_published(tmp_path):
    root = fixture(tmp_path)
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.prepare(root, StubGitHub(root), "keep")
    message = str(exc.value)
    assert "protocolCertification.ledger stays pending" in message
    # Committed working pins versus the recorded published package commits.
    for source, component_sha, published_sha in (
        ("sdk-dotnet", "6ba49ec32ea846c64bc2094807761d4884dbc4bf", "a88a7fbb3643cb046e70d6ef4d38ae70a025a2a4"),
        ("sdk-python", "40ecf7318573214fb6c702b12ebd56b3ad47ba60", "f7930b6e9c3ce47ade148bba3d4510eeffd2ccc4"),
        ("sdk-js", "d7cec2d510e053fc86252b125bde21313a7e6e7c", "c99e71197dd940ed952aecb024c6de273456f2ae"),
    ):
        assert source in message
        assert component_sha in message
        assert published_sha in message
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert yaml.safe_load(before)["protocolCertification"]["ledger"] == {
        "status": "pending",
        "repository": "honua-io/honua-evidence",
        "commit": "pending",
        "requirementsSourceRevision": "pending",
        "path": "data/protocol-certification.v1.json",
        "sha256": "pending",
    }


def test_cli_refuses_divergent_sdk_pins_without_writing_a_plan(tmp_path):
    plan_path = tmp_path / "rebind-plan.json"
    assert MODULE.main(["--plan-output", str(plan_path)]) == 1
    assert not plan_path.exists()


def test_plan_targets_published_sdk_pins_when_components_match(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    pins = {row["source"]: (row["target"], row["rule"]) for row in plan["sources"]}
    manifest = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))
    for source, component, artifact in MODULE.SDK_PRODUCERS:
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        assert pins[source] == (published, "manifest/frozen")
        assert manifest["components"][component]["sha"] == published
    assert pins["server-certification"] == ("87966c3f7b6c840ffc4d4da0b451714ab717b18a", "manifest/frozen")
    assert plan["receipt_schema_min"] == {"current": "v2", "proposed": "v2"}
    assert plan["bindings"]["PROTOCOL_CERTIFICATION_MATRIX_COMMIT"]["current"] == "pending"
    assert manifest["protocolCertification"]["ledger"]["status"] == "pending"


def test_apply_changes_only_planned_repository_files(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, payloads, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "", "returncode": 0})())
    # Assert the complete intended payload rather than mutating the real worktree.
    assert set(payloads) == {str(MODULE.REVISIONS), str(MODULE.CATALOG), "certification/generate-protocol-requirements.py", *(p for mappings in MODULE.VENDORED.values() for _, p in mappings)}
    assert tuple(MODULE.CALLERS) == (Path(".github/workflows/pr-protocol-certification.yml"), Path(".github/workflows/nightly-protocol-certification.yml"), Path(".github/workflows/release-train.yml"))


@pytest.mark.parametrize("ledger_status", ["pending", "bound"])
def test_finalize_updates_manifest_pins_and_receipt_together(tmp_path, ledger_status):
    root = fixture(tmp_path)
    align_components_to_published(root)
    manifest_path = root / MODULE.MANIFEST
    manifest = manifest_path.read_text()
    current = yaml.safe_load(manifest)["protocolCertification"]["ledger"]["status"]
    manifest_path.write_text(MODULE.replace_scalar(manifest, "status", current, ledger_status))
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    requirements = "a" * 40
    evidence = "b" * 40
    digest = "sha256:" + "c" * 64

    MODULE.finalize(root, plan, requirements, evidence, digest)

    manifest = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    assert yaml.safe_load(manifest)["protocolCertification"]["ledger"]["status"] == "bound"
    assert MODULE.scalar(manifest, "commit") == evidence
    assert MODULE.scalar(manifest, "requirementsSourceRevision") == requirements
    assert MODULE.scalar(manifest, "sha256") == digest
    for caller in MODULE.CALLERS:
        assert f"gate-protocol-certification.yml@{requirements}" in (root / caller).read_text()
    receipt = (root / "rebind-receipt.md").read_text()
    for value in (requirements, evidence, digest, "Closes #191", "Refs #180 #181 #182 #187 #188"):
        assert value in receipt
    assert "merged with a merge commit (not squash- or rebase-merged)" in receipt


def test_finalize_does_not_bind_when_published_pins_differ(tmp_path):
    root = fixture(tmp_path)
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    plan = {"sources": [], "bindings": {}, "receipt_schema_min": {"current": "v2", "proposed": "v2"}}
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "sha256:" + "c" * 64)
    assert "stays pending" in str(exc.value)
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert not (root / "rebind-receipt.md").exists()


def test_finalize_rejects_plan_target_that_is_not_the_published_pin(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    manifest = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    plan = {"sources": [], "bindings": {}, "receipt_schema_min": {"current": "v2", "proposed": "v2"}}
    for source, _component, artifact in MODULE.SDK_PRODUCERS:
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        target = "f" * 40 if source == "sdk-js" else published
        plan["sources"].append({"source": source, "current": published, "target": target, "rule": "manifest/frozen"})
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "sha256:" + "c" * 64)
    assert "not published clientArtifacts.honua-sdk-js.sourceSha" in str(exc.value)
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert yaml.safe_load(before)["protocolCertification"]["ledger"]["status"] == "pending"


def test_finalize_rejects_noncanonical_fingerprints(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    try:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "not-a-digest")
    except MODULE.Finding as exc:
        assert "sha256" in str(exc)
    else:
        raise AssertionError("noncanonical digest was accepted")
