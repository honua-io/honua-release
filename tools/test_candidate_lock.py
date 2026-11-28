"""A frozen lock binds a run to the candidate only after it is verified against the manifest the run deploys."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "e2e"))
import candidate_lock  # noqa: E402
import cloud_journey  # noqa: E402
import rc_gate_window as w  # noqa: E402

SHA, DIGEST = "b" * 40, "sha256:" + "2" * 64
MANIFEST = {"components": {"honua-server": {"image": "ghcr.io/honua-io/honua-server:candidate",
                                            "digest": DIGEST, "sha": SHA}}}


def _write_candidate(tmp_path, *, digest=DIGEST, revision=SHA):
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    lock = {"sourceInputs": {"platformManifest": {"sha256": "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()}},
            "components": {"honua-server": {"artifacts": [{"kind": "image", "coordinate": "ghcr.io/honua-io/honua-server",
                                                           "digest": digest, "sourceRevision": revision}]}}}
    path = tmp_path / "platform-lock.json"
    path.write_bytes(json.dumps(lock).encode())
    return manifest, path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _verify(manifest, lock, digest):
    return candidate_lock.main(["--lock", str(lock), "--digest", digest, "--manifest", str(manifest)])


def test_the_dispatched_lock_of_this_manifest_is_accepted(tmp_path):
    assert _verify(*_write_candidate(tmp_path)) == 0


@pytest.mark.parametrize("change", [dict(digest="sha256:" + "3" * 64), dict(revision="c" * 40)])
def test_a_lock_naming_another_server_image_is_refused(tmp_path, change):
    assert _verify(*_write_candidate(tmp_path, **change)) == 1


def test_a_lock_that_is_not_the_dispatched_digest_is_refused(tmp_path):
    manifest, lock, _ = _write_candidate(tmp_path)
    assert _verify(manifest, lock, "sha256:" + "f" * 64) == 1


def test_a_lock_of_other_manifest_bytes_is_refused(tmp_path):
    manifest, lock, digest = _write_candidate(tmp_path)
    manifest.write_text(manifest.read_text() + "# edited\n", encoding="utf-8")
    assert _verify(manifest, lock, digest) == 1


def _cloud_report(lock_digest):
    report = {"gate": "cloud-parity", "status": "pass", "certifying": True, "certifyingScope": True,
              "server": {"sourceSha": SHA, "image": f"ghcr.io/honua-io/honua-server@{DIGEST}"}}
    report.update(cloud_journey.candidate_binding(MANIFEST, lock_digest))
    return report


def test_a_cloud_report_binds_only_the_verified_lock():
    lock = "sha256:" + "c" * 64
    candidate = w.Candidate(SHA, DIGEST, lock)
    assert w.matches_candidate(_cloud_report(lock), candidate)
    assert not w.matches_candidate(_cloud_report("sha256:" + "d" * 64), candidate)


def test_a_cloud_report_missing_the_lock_never_binds():
    report = _cloud_report("")
    assert "platformLockDigest" not in report and "honua-server" not in report
    assert not any(w.matches_candidate(report, w.Candidate(SHA, DIGEST, "sha256:" + c * 64)) for c in "0123456789abcdef")


def test_a_malformed_lock_digest_is_refused():
    with pytest.raises(ValueError):
        cloud_journey.candidate_binding(MANIFEST, "sha256:short")


CONSOLE_SHA = "a" * 40
CONSOLE_DIGEST = "sha256:" + "4" * 64
CONSOLE_VERSION = "0.0.0-trunk." + CONSOLE_SHA[:12]
CONSOLE_COORD = "https://github.com/honua-io/honua-console/actions/artifacts/console-static-release"


def _static_bundle_manifest(**console_overrides):
    console = {
        "repository": "https://github.com/honua-io/honua-console",
        "artifact": "actions-artifact:console-static-release",
        "sha": CONSOLE_SHA,
        "artifactSourceRevision": CONSOLE_SHA,
        "artifactSha256": CONSOLE_DIGEST,
        "artifactVersion": CONSOLE_VERSION,
    }
    console.update(console_overrides)
    return {"components": {**MANIFEST["components"], "honua-console": console}}


def _write_bundle_candidate(tmp_path, *, bundle_artifacts=None):
    manifest = tmp_path / "platform-manifest.yaml"
    manifest.write_text(yaml.safe_dump(_static_bundle_manifest()), encoding="utf-8")
    artifacts = bundle_artifacts if bundle_artifacts is not None else [{
        "kind": "static-bundle",
        "coordinate": CONSOLE_COORD,
        "sha256": CONSOLE_DIGEST,
        "sourceRevision": CONSOLE_SHA,
        "version": CONSOLE_VERSION,
    }]
    lock = {
        "sourceInputs": {"platformManifest": {"sha256": "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()}},
        "components": {
            "honua-server": {"artifacts": [{"kind": "image", "coordinate": "ghcr.io/honua-io/honua-server",
                                            "digest": DIGEST, "sourceRevision": SHA}]},
            "honua-console": {"artifacts": artifacts},
        },
    }
    path = tmp_path / "platform-lock.json"
    path.write_bytes(json.dumps(lock).encode())
    return manifest, path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_static_bundle_component_pin_and_lock_are_accepted(tmp_path):
    manifest, lock, digest = _write_bundle_candidate(tmp_path)
    assert candidate_lock.main([
        "--lock", str(lock), "--digest", digest, "--manifest", str(manifest),
        "--component", "server", "--component", "console",
    ]) == 0
    pins = candidate_lock.resolve_pins(_static_bundle_manifest(), ("server", "console"))
    assert pins["console"] == {
        "kind": "static-bundle", "coordinate": CONSOLE_COORD,
        "sha": CONSOLE_SHA, "digest": CONSOLE_DIGEST, "version": CONSOLE_VERSION,
    }
    assert pins["server"]["kind"] == "image"


def test_static_bundle_lock_accepts_omitted_version_when_other_fields_match(tmp_path):
    artifact = {"kind": "static-bundle", "coordinate": CONSOLE_COORD,
                "sha256": CONSOLE_DIGEST, "sourceRevision": CONSOLE_SHA}
    manifest, lock, digest = _write_bundle_candidate(tmp_path, bundle_artifacts=[artifact])
    assert candidate_lock.main([
        "--lock", str(lock), "--digest", digest, "--manifest", str(manifest),
        "--component", "console",
    ]) == 0


@pytest.mark.parametrize("bundle_artifacts", [
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "sha256": "sha256:" + "9" * 64,
      "sourceRevision": CONSOLE_SHA, "version": CONSOLE_VERSION}],
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "sha256": CONSOLE_DIGEST,
      "sourceRevision": "c" * 40, "version": CONSOLE_VERSION}],
    [{"kind": "static-bundle", "coordinate": "https://github.com/other/honua-console/actions/artifacts/console-static-release",
      "sha256": CONSOLE_DIGEST, "sourceRevision": CONSOLE_SHA, "version": CONSOLE_VERSION}],
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "sha256": CONSOLE_DIGEST,
      "sourceRevision": CONSOLE_SHA, "version": "0.0.0-trunk.000000000000"}],
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "sha256": CONSOLE_DIGEST,
      "sourceRevision": CONSOLE_SHA, "version": ""}],
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "digest": CONSOLE_DIGEST,
      "sourceRevision": CONSOLE_SHA, "version": CONSOLE_VERSION}],
    [{"kind": "image", "coordinate": "ghcr.io/honua-io/honua-console", "digest": CONSOLE_DIGEST,
      "sourceRevision": CONSOLE_SHA}],
    [{"kind": "static-bundle", "coordinate": CONSOLE_COORD, "sha256": CONSOLE_DIGEST, "sourceRevision": CONSOLE_SHA}] * 2,
    [],
])
def test_static_bundle_lock_mismatch_is_refused(tmp_path, bundle_artifacts):
    manifest, lock, digest = _write_bundle_candidate(tmp_path, bundle_artifacts=bundle_artifacts)
    assert candidate_lock.main([
        "--lock", str(lock), "--digest", digest, "--manifest", str(manifest),
        "--component", "console",
    ]) == 1


@pytest.mark.parametrize("override", [
    {"artifactSha256": ""},
    {"artifactSha256": "sha256:short"},
    {"sha": "a" * 39},
    {"artifactSourceRevision": "c" * 40},
    {"artifactSourceRevision": ""},
    {"artifactVersion": ""},
    {"artifactVersion": "   "},
    {"artifactVersion": "pre-release"},
    {"artifactVersion": " " + CONSOLE_VERSION},
    {"repository": "https://github.com/other/honua-console"},
    {"artifact": "actions-artifact:"},
    {"artifact": "actions-artifact:.."},
    {"artifact": "actions-artifact:nested/name"},
    {"image": "ghcr.io/honua-io/honua-console:candidate"},
])
def test_static_bundle_manifest_pin_refuses_invalid_or_foreign_fields(override):
    with pytest.raises(ValueError):
        candidate_lock.resolve_pins(_static_bundle_manifest(**override), ("console",))


def test_live_manifest_binds_qualification_lock_for_console_and_server():
    import generate_platform_lock as generator
    from platform_lock_bundle import canonical_bytes

    manifest_path = ROOT / "platform-manifest.yaml"
    matrix_path = ROOT / "compatibility-matrix.yaml"
    draft = generator.generate(manifest_path, matrix_path, qualification=True, image_inspector=None)
    assert not draft.unresolved
    manifest_bytes = manifest_path.read_bytes()
    pins = candidate_lock.resolve_pins(yaml.safe_load(manifest_bytes), ("console", "server"))
    lock_bytes = canonical_bytes(draft.lock)
    lock_digest = "sha256:" + hashlib.sha256(lock_bytes).hexdigest()
    candidate_lock.validate_lock(lock_bytes, lock_digest, candidate_lock.PinnedCandidate(manifest_bytes, pins))
