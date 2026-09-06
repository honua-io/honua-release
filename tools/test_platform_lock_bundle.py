"""Independent artifact identity expectations and fail-closed train boundary tests."""
import base64
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import platform_lock_bundle as bundle
import release_inspect
from test_platform_lock import valid_lock, REVISION

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path):
    lock = valid_lock()
    # The bytes and expected digests are external to the BOM implementation.
    data = b"a fixture of published package bytes\n"
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    manifest = {"platformRelease": "2026.1-rc.1", "status": "rc", "components": {}}
    for name, comp in lock["components"].items():
        comp["artifactIdentityModel"] = "published"
        comp["artifacts"] = [{"kind": "npm", "coordinate": f"@honua/{name}",
                              "version": "1.2.3", "sourceRevision": "c" * 40,
                              "integrity": integrity}]
        # SDK baseline declarations describe the published artifact revision.
        if "serverCompatibility" in comp:
            declaration = copy.deepcopy(comp["serverCompatibility"]["declarations"][0])
            declaration["revision"] = "c" * 40
            comp["serverCompatibility"]["declarations"] = [declaration]
        manifest["components"][name] = {
            "repository": comp["source"]["repository"], "sha": REVISION,
            "lifecycleStatus": "GA", "artifact": f"npm:@honua/{name}", "version": "1.2.3",
            "artifactSourceRevision": "c" * 40,
            "serverCompatibility": comp.get("serverCompatibility"),
        }
    manifest["clientArtifacts"] = {
        name: {"integrity": integrity, "sourceSha": "c" * 40}
        for name in lock["components"]
    }
    paths = [tmp_path / "manifest.yaml", tmp_path / "matrix.yaml"]
    paths[0].write_text(yaml.safe_dump(manifest))
    paths[1].write_text("contracts: {}\n")
    lock["sourceInputs"] = {
        key: {"path": path.name, "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        for key, path in zip(("platformManifest", "compatibilityMatrix"), paths)
    }
    return lock, paths, data


def test_bundle_keeps_published_identity_independent_of_source_head(candidate, tmp_path):
    lock, paths, data = candidate
    bundle.bind(lock, *paths, "2026.1-rc.1")
    bom = bundle.build_bom(lock)
    entry = next(c for c in bom["components"] if c["name"] == "sdk")
    assert entry["version"] == "1.2.3"
    assert entry["purl"] == "pkg:npm/%40honua/sdk@1.2.3"
    assert entry["hashes"] == [{"alg": "SHA-512", "content": hashlib.sha512(data).hexdigest()}]
    props = {p["name"]: p["value"] for p in entry["properties"]}
    assert props["honua:artifactSourceRevision"] == "c" * 40
    assert props["honua:componentSourceRevision"] == "a" * 40
    assert props["honua:coordinate"] == "@honua/sdk"
    assert props["honua:lifecycleStatus"] == "GA"
    expected_digest = "sha256:" + hashlib.sha256(json.dumps(
        lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert bom["metadata"]["component"]["version"] == "honua-2026.1-rc.1"
    assert bom["metadata"]["component"]["bom-ref"] == expected_digest
    files = bundle.bundle_files(lock)
    assert files["platform-lock.json"] == json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
    assert json.loads(files["platform-release.v1.json"])["lockDigest"] == expected_digest
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_bytes(files["compatibility-ledger.v1.json"])
    ledger = release_inspect.load_ledger(ledger_path)
    assert ledger["platformLocks"][expected_digest]["releaseArtifacts"] == [
        {"component": name, "artifactIndex": 0} for name in sorted(lock["components"])]
    assert ledger["platformLocks"][expected_digest]["certifications"] == []
    assert not release_inspect.inspect(lock, ledger)["certified"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda lock: lock["platform"].update(id="honua-2026.1-rc.2"), "platform label"),
    (lambda lock: lock["platform"].update(status="draft"), "rc status"),
    (lambda lock: lock["components"].pop("sdk"), "denominator"),
    (lambda lock: lock["components"]["sdk"]["artifacts"][0].update(version="9.9.9"), "frozen input"),
    (lambda lock: lock["components"]["sdk"]["artifacts"][0].update(sourceRevision="d" * 40), "frozen input"),
    (lambda lock: lock.update(notes="TBD"), "placeholder"),
])
def test_rejects_wrong_candidate_and_incomplete_lock(candidate, mutation, reason):
    lock, paths, _ = candidate
    mutation(lock)
    with pytest.raises(ValueError, match=reason):
        bundle.bind(lock, *paths, "2026.1-rc.1")


def test_manifest_bytes_cannot_move_after_freeze(candidate):
    lock, paths, _ = candidate
    paths[0].write_text(paths[0].read_text() + "# edited after freeze\n")
    with pytest.raises(ValueError, match="sourceInputs.platformManifest.sha256"):
        bundle.bind(lock, *paths, "2026.1-rc.1")


def test_requested_patch_candidate_identity_is_preserved(candidate):
    lock, paths, _ = candidate
    manifest = yaml.safe_load(paths[0].read_text())
    manifest["platformRelease"] = "2026.1.0-rc.1"
    paths[0].write_text(yaml.safe_dump(manifest))
    lock["platform"]["id"] = "honua-2026.1.0-rc.1"
    lock["sourceInputs"]["platformManifest"]["sha256"] = "sha256:" + hashlib.sha256(paths[0].read_bytes()).hexdigest()
    bundle.bind(lock, *paths, "2026.1.0-rc.1")
    assert bundle.build_bom(lock)["metadata"]["component"]["version"] == "honua-2026.1.0-rc.1"


def test_pending_published_client_is_never_omitted(candidate):
    lock, paths, _ = candidate
    manifest = yaml.safe_load(paths[0].read_text())
    manifest["components"]["sdk"]["pendingPublishedClients"] = {"python": "publication-issue"}
    paths[0].write_text(yaml.safe_dump(manifest))
    lock["sourceInputs"]["platformManifest"]["sha256"] = "sha256:" + hashlib.sha256(paths[0].read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="published package coordinate is pending"):
        bundle.bind(lock, *paths, "2026.1-rc.1")


def test_cli_rejects_tampering_and_refuses_to_replace_identity(candidate, tmp_path):
    lock, paths, _ = candidate
    source = tmp_path / "source.json"
    source.write_text(json.dumps(lock))
    output = tmp_path / "bundle"
    command = [sys.executable, str(ROOT / "tools/platform_lock_bundle.py"), str(source),
               "--manifest", str(paths[0]), "--matrix", str(paths[1]),
               "--label", "2026.1-rc.1", "--out-dir", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 0
    (output / "bom.cdx.json").write_text('{"components": []}')
    check = subprocess.run(command + ["--check"], capture_output=True, text=True)
    assert check.returncode == 1 and "bom.cdx.json: bytes differ" in check.stderr
    write = subprocess.run(command, capture_output=True, text=True)
    assert write.returncode == 1 and "refusing to overwrite" in write.stderr


def test_all_artifacts_and_image_architecture_metadata_appear(candidate):
    lock, _, _ = candidate
    image = {"kind": "image", "coordinate": "ghcr.io/honua-io/server", "version": "2.3.4",
             "sourceRevision": "e" * 40, "digest": "sha256:" + "f" * 64,
             "platformDigests": {"amd64": "sha256:" + "1" * 64, "arm64": "sha256:" + "2" * 64},
             "architectures": ["amd64", "arm64"]}
    lock["components"]["sdk"]["artifacts"].append(image)
    entries = [c for c in bundle.build_bom(lock)["components"] if c["name"] == "sdk"]
    assert len(entries) == 2
    assert entries[1]["version"] == "2.3.4"
    assert entries[1]["hashes"] == [{"alg": "SHA-256", "content": "f" * 64}]
    props = {p["name"]: p["value"] for p in entries[1]["properties"]}
    assert json.loads(props["honua:platformDigests"]) == image["platformDigests"]
    assert json.loads(props["honua:architectures"]) == ["amd64", "arm64"]


def test_workflows_require_signed_freeze_output_before_promotion():
    train = yaml.safe_load((ROOT / ".github/workflows/release-train.yml").read_text())
    freeze = train["jobs"]["freeze"]
    assert freeze["permissions"]["attestations"] == "write"
    steps = freeze["steps"]
    prepare = next(i for i, s in enumerate(steps) if "platform_lock_bundle.py" in s.get("run", ""))
    sign = next(i for i, s in enumerate(steps) if "actions/attest-build-provenance@" in s.get("uses", ""))
    assert prepare < sign
    assert steps[sign]["with"]["subject-path"] == "frozen-lock/platform-lock.json"
    report = "\n".join(s.get("run", "") for s in train["jobs"]["report"]["steps"])
    assert "cp candidate-input/frozen-lock/* out/certified-candidate/" in report
    assert "cp platform-lock.json" not in report
    promote = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    steps = promote["jobs"]["promote"]["steps"]
    verify = next(i for i, s in enumerate(steps) if "gh attestation verify candidate/platform-lock.json" in s.get("run", ""))
    finalize = next(i for i, s in enumerate(steps) if "finalize_release.py" in s.get("run", ""))
    assert verify < finalize
    command = steps[verify]["run"]
    for flag in ("--bundle", "--signer-workflow", "--source-digest", "--source-ref", "--check"):
        assert flag in command
    assert "generate_bom.py" not in steps[finalize]["run"]
