from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from generate_compatibility_table import main as table_main
from sdk_baselines import SDK_COMPONENTS, findings
from test_platform_lock import valid_lock
from verify_sdk_baseline_sources import SourceReader, main, source_identity, verify_sources


def canonical_hash(value):
    # Independent fixture calculation from the documented canonical JSON encoding.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def sources(tmp_path):
    lock = valid_lock()
    root = tmp_path / "sources"
    content = lock["components"][SDK_COMPONENTS[0]]["serverCompatibility"]["manifests"][0]["content"]
    declaration_bytes = b'{"minimumServerVersion":"1.2.0"}\n'
    revisions = {}
    for name in ("honua-server", *SDK_COMPONENTS):
        repo = root / "honua-io" / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Mike McDougall")
        git(repo, "config", "user.email", "mike@honua.io")
        (repo / "capabilities.json").write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        (repo / "compatibility.json").write_bytes(declaration_bytes)
        git(repo, "add", ".")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "Record independent baseline fixture")
        revisions[name] = git(repo, "rev-parse", "HEAD")
    for name in SDK_COMPONENTS:
        component = lock["components"][name]
        component["source"]["revision"] = revisions[name]
        baseline = component["serverCompatibility"]
        manifest = baseline["manifests"][0]
        manifest["source"]["revision"] = revisions["honua-server"]
        manifest["sha256"] = canonical_hash(content)
        baseline["declarations"][0].update(
            revision=revisions[name], sha256="sha256:" + hashlib.sha256(declaration_bytes).hexdigest())
    return lock, root


def test_real_commit_sources_verify_all_four_sdks_and_independent_floor(sources):
    lock, root = sources
    assert findings(lock) == []
    assert verify_sources(lock, SourceReader(root)) == list(SDK_COMPONENTS)
    assert {item["serverCompatibility"]["minimumServerVersion"] for name, item in lock["components"].items()
            if name in SDK_COMPONENTS} == {"1.2.0"}  # max(1.0.0, 1.2.0), excluding optional 9.0.0


def test_self_consistent_forged_manifest_is_rejected(sources):
    lock, root = sources
    baseline = lock["components"]["honua-sdk-js"]["serverCompatibility"]
    manifest = baseline["manifests"][0]
    manifest["content"]["capabilities"]["admin.write"]["minimumServerVersion"] = "0.1.0"
    manifest["sha256"] = canonical_hash(manifest["content"])
    baseline["minimumServerVersion"] = "1.0.0"
    baseline["declarations"][0]["minimumServerVersion"] = "1.0.0"
    assert findings(lock) == []  # The previous strict checker accepted this fabricated floor.
    with pytest.raises(ValueError, match="pinned manifest source disagrees"):
        verify_sources(lock, SourceReader(root))


def test_forged_declaration_digest_is_rejected(sources):
    lock, root = sources
    declaration = lock["components"]["honua-sdk-js"]["serverCompatibility"]["declarations"][0]
    declaration["sha256"] = "sha256:" + hashlib.sha256(b'altered SDK source').hexdigest()
    assert findings(lock) == []
    with pytest.raises(ValueError, match="pinned declaration byte digest disagrees"):
        verify_sources(lock, SourceReader(root))


def test_working_tree_and_new_head_cannot_replace_pinned_commit(sources):
    lock, root = sources
    for name in ("honua-server", *SDK_COMPONENTS):
        repo = root / "honua-io" / name
        (repo / "capabilities.json").write_text("{}", encoding="utf-8")
        (repo / "compatibility.json").write_text("altered source", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "Advance fixture source")
    assert verify_sources(lock, SourceReader(root)) == list(SDK_COMPONENTS)


@pytest.mark.parametrize("missing", ["revision", "file"])
def test_missing_source_fails_closed(sources, missing):
    lock, root = sources
    source = lock["components"]["honua-sdk-js"]["serverCompatibility"]["manifests"][0]["source"]
    source[missing if missing == "revision" else "path"] = "f" * 40 if missing == "revision" else "missing.json"
    with pytest.raises(ValueError, match="cannot read pinned source"):
        verify_sources(lock, SourceReader(root))


@pytest.mark.parametrize("path", ["/tmp/file", "../file", "x/../file", "x//file", "x\\file", "x\nfile"])
def test_source_path_must_address_a_repository_file(path):
    with pytest.raises(ValueError, match="relative repository file"):
        source_identity("https://github.com/honua-io/honua-server", "a" * 40, path)


def test_cli_strict_table_check_reads_sources_and_fails_after_forgery(sources, tmp_path):
    lock, root = sources
    source, output = tmp_path / "lock.yaml", tmp_path / "table.md"
    source.write_text(yaml.safe_dump(lock), encoding="utf-8")
    args = [str(source), "--output", str(output), "--source-root", str(root)]
    assert main([str(source), "--source-root", str(root)]) == 0
    assert table_main(args) == 0
    assert table_main([*args, "--check"]) == 0
    lock["components"]["honua-sdk-js"]["serverCompatibility"]["declarations"][0]["sha256"] = "sha256:" + "0" * 64
    source.write_text(yaml.safe_dump(lock), encoding="utf-8")
    assert table_main(args) == 0
    assert table_main([*args, "--check-output"]) == 0
    assert table_main([*args, "--check"]) == 1
    assert main([str(source), "--source-root", str(root)]) == 1


def test_live_freeze_verifies_sources_before_artifact_certification():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/release-train.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["freeze"]["steps"] if step.get("id") == "candidate")
    live = script.split('if [ "$DRY_RUN" = "false" ]; then')[2].split("fi")[0]
    assert "python tools/verify_sdk_baseline_sources.py platform-lock.json" in live
    assert script.index("verify_sdk_baseline_sources.py") < script.index("verify_client_artifacts.py")
