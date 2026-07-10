"""Tests for the certified candidate artifact/source binding."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_binding as cb  # noqa: E402

IDENTITY = {
    "source_repository": "honua-io/honua-release",
    "source_sha": "a" * 40,
    "workflow_path": ".github/workflows/release-train.yml",
    "train_run_id": "28720697360",
    "train_run_attempt": 2,
    "train_run_url": "https://github.com/honua-io/honua-release/actions/runs/28720697360",
}


def _files(root: Path, manifest: bytes = b"platformRelease: 2026.1-rc.1\n") -> tuple[Path, Path]:
    root.mkdir()
    manifest_path = root / cb.PLATFORM_MANIFEST
    matrix_path = root / cb.COMPATIBILITY_MATRIX
    manifest_path.write_bytes(manifest)
    matrix_path.write_bytes(b"matrixVersion: 1\n")
    return manifest_path, matrix_path


def _bound_report(manifest_path: Path, matrix_path: Path) -> dict:
    return cb.bind_gate_report(
        {"platform_label": "2026.1-rc.1", "overallStatus": "pass", "gates": []},
        manifest_path,
        matrix_path,
        **IDENTITY,
    )


def _verify(report: dict, manifest_path: Path, matrix_path: Path, **overrides) -> tuple[bool, str]:
    identity = dict(IDENTITY)
    identity.update(overrides)
    return cb.verify_candidate_binding(report, manifest_path, matrix_path, **identity)


def test_bound_candidate_verifies_against_exact_bytes_and_identity(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    ok, why = _verify(_bound_report(manifest_path, matrix_path), manifest_path, matrix_path)
    assert ok, why


def test_post_certification_mutation_is_refused(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    report = _bound_report(manifest_path, matrix_path)

    manifest_path.write_bytes(manifest_path.read_bytes() + b"status: changed\n")

    ok, why = _verify(report, manifest_path, matrix_path)
    assert not ok
    assert "platform-manifest.yaml" in why
    assert "mismatch" in why


def test_same_name_same_size_artifact_substitution_is_refused(tmp_path: Path):
    certified_manifest, matrix_path = _files(
        tmp_path / "certified",
        manifest=b"platformRelease: 2026.1-rc.1\n",
    )
    report = _bound_report(certified_manifest, matrix_path)
    substituted_manifest, _ = _files(
        tmp_path / "substituted",
        manifest=b"platformRelease: 2026.1-rc.9\n",
    )
    assert substituted_manifest.stat().st_size == certified_manifest.stat().st_size

    ok, why = _verify(report, substituted_manifest, matrix_path)
    assert not ok
    assert "platform-manifest.yaml" in why
    assert "SHA-256 mismatch" in why


def test_source_or_train_substitution_is_refused(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    report = _bound_report(manifest_path, matrix_path)

    ok, why = _verify(report, manifest_path, matrix_path, source_sha="b" * 40)
    assert not ok
    assert "source.sha" in why

    ok, why = _verify(
        report,
        manifest_path,
        matrix_path,
        train_run_id="28720697361",
        train_run_url="https://github.com/honua-io/honua-release/actions/runs/28720697361",
    )
    assert not ok
    assert "train.runId" in why


def test_missing_binding_is_refused(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    ok, why = _verify({"overallStatus": "pass"}, manifest_path, matrix_path)
    assert not ok
    assert "no candidate binding" in why


def test_create_bundle_copies_exact_candidate_bytes_and_binds_copies(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "input")
    report_path = tmp_path / "gate-report.json"
    report_path.write_text(json.dumps({"overallStatus": "pass"}), encoding="utf-8")
    out_dir = tmp_path / "certified-candidate"

    bound_report_path = cb.create_bundle(
        report_path,
        manifest_path,
        matrix_path,
        out_dir,
        **IDENTITY,
    )

    bundled_manifest = out_dir / cb.PLATFORM_MANIFEST
    bundled_matrix = out_dir / cb.COMPATIBILITY_MATRIX
    assert bundled_manifest.read_bytes() == manifest_path.read_bytes()
    assert bundled_matrix.read_bytes() == matrix_path.read_bytes()
    report = json.loads(bound_report_path.read_text(encoding="utf-8"))
    ok, why = _verify(report, bundled_manifest, bundled_matrix)
    assert ok, why
