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
