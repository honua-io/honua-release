"""Tests for the certified candidate artifact/source binding."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_binding as cb  # noqa: E402

IDENTITY = {
    "source_repository": "honua-io/honua-release",
    "source_sha": "a" * 40,
    "source_branch": "trunk",
    "workflow_path": ".github/workflows/release-train.yml",
    "train_run_id": "28720697360",
    "train_run_attempt": 2,
    "train_run_url": "https://github.com/honua-io/honua-release/actions/runs/28720697360",
    "certification_mode": "live",
}


def _files(root: Path, manifest: bytes = b"platformRelease: 2026.1-rc.1\n") -> tuple[Path, Path]:
    root.mkdir()
    manifest_path = root / cb.PLATFORM_MANIFEST
    matrix_path = root / cb.COMPATIBILITY_MATRIX
    manifest_path.write_bytes(manifest)
    matrix_path.write_bytes(b"matrixVersion: 1\n")
    return manifest_path, matrix_path


def _bound_report(manifest_path: Path, matrix_path: Path) -> dict:
    gates = [{"gate": name, "status": "pass"} for name in sorted(cb.REQUIRED_RELEASE_GATES)]
    return cb.bind_gate_report(
        {"platform_label": "2026.1-rc.1", "dry_run": False, "overallStatus": "pass",
         "generatedAt": datetime.now(timezone.utc).isoformat(), "gates": gates},
        manifest_path,
        matrix_path,
        **IDENTITY,
    )


def _live_report(now: datetime) -> dict:
    return {"dry_run": False, "overallStatus": "pass", "generatedAt": now.isoformat(),
            "gates": [{"gate": gate, "status": "pass"}
                      for gate in sorted(cb.REQUIRED_RELEASE_GATES)]}


def test_live_report_rejects_a_skipped_or_missing_required_cell():
    now = datetime.now(timezone.utc)
    report = _live_report(now)
    report["gates"][0]["status"] = "skipped"
    ok, why = cb.validate_live_report(report, now=now)
    assert not ok and "skip/blocked/fail is RED" in why

    report = _live_report(now)
    report["gates"].pop()
    ok, why = cb.validate_live_report(report, now=now)
    assert not ok and "missing required gate" in why


def test_live_report_rejects_stale_receipt():
    now = datetime.now(timezone.utc)
    report = _live_report(now - timedelta(hours=cb.LIVE_REPORT_MAX_AGE_HOURS + 1))
    ok, why = cb.validate_live_report(report, now=now)
    assert not ok and "stale" in why and "max-age" in why


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


def test_dry_run_certification_is_refused_for_live_promotion(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    dry_run_identity = dict(IDENTITY, certification_mode="dry-run")
    report = cb.bind_gate_report(
        {"platform_label": "2026.1-rc.1", "dry_run": True, "overallStatus": "pass", "gates": []},
        manifest_path,
        matrix_path,
        **dry_run_identity,
    )

    ok, why = _verify(report, manifest_path, matrix_path, certification_mode="live")
    assert not ok
    assert "certificationMode" in why


def test_non_default_branch_train_metadata_is_refused():
    repository = {
        "full_name": "honua-io/honua-release",
        "default_branch": "trunk",
    }
    branch = {"name": "trunk", "protected": True}
    run = {
        "id": 28720697360,
        "run_attempt": 2,
        "html_url": "https://github.com/honua-io/honua-release/actions/runs/28720697360",
        "repository": {"full_name": "honua-io/honua-release"},
        "head_repository": {"full_name": "honua-io/honua-release"},
        "head_branch": "trunk",
        "head_sha": "a" * 40,
        "path": ".github/workflows/release-train.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }
    ok, why, identity = cb.validate_train_run_metadata(
        run,
        repository,
        branch,
        expected_repository="honua-io/honua-release",
        expected_workflow_path=".github/workflows/release-train.yml",
        expected_run_id="28720697360",
    )
    assert ok, why
    assert identity is not None
    assert identity["source_branch"] == "trunk"

    run["head_branch"] = "feature/weaken-release-gates"
    ok, why, identity = cb.validate_train_run_metadata(
        run,
        repository,
        branch,
        expected_repository="honua-io/honua-release",
        expected_workflow_path=".github/workflows/release-train.yml",
        expected_run_id="28720697360",
    )
    assert not ok
    assert "default branch" in why
    assert identity is None

    run["head_branch"] = "trunk"
    branch["protected"] = False
    ok, why, identity = cb.validate_train_run_metadata(
        run,
        repository,
        branch,
        expected_repository="honua-io/honua-release",
        expected_workflow_path=".github/workflows/release-train.yml",
        expected_run_id="28720697360",
    )
    assert not ok
    assert "not protected" in why
    assert identity is None


def test_release_promotion_environment_requires_exact_human_reviewer_roster():
    environment = {
        "name": "release-promotion",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 12301237, "login": "mikemcdougall"}},
                ],
            },
        ],
    }
    ok, why = cb.validate_environment_metadata(
        environment,
        expected_name="release-promotion",
        expected_reviewer_ids=[12301237],
    )
    assert ok, why

    environment["protection_rules"] = []
    ok, why = cb.validate_environment_metadata(
        environment,
        expected_name="release-promotion",
        expected_reviewer_ids=[12301237],
    )
    assert not ok
    assert "required-reviewer" in why


def test_release_promotion_environment_accepts_an_exact_multi_reviewer_roster():
    environment = {
        "name": "release-promotion",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 12301237, "login": "mikemcdougall"}},
                    {"type": "User", "reviewer": {"id": 99, "login": "standby-owner"}},
                ],
            },
        ],
    }

    ok, why = cb.validate_environment_metadata(
        environment,
        expected_name="release-promotion",
        expected_reviewer_ids=[12301237, 99],
    )

    assert ok, why


def test_release_promotion_environment_rejects_an_empty_expected_roster():
    environment = {
        "name": "release-promotion",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 12301237, "login": "mikemcdougall"}},
                ],
            },
        ],
    }

    ok, why = cb.validate_environment_metadata(
        environment,
        expected_name="release-promotion",
        expected_reviewer_ids=[],
    )

    assert not ok
    assert "at least one" in why


def test_release_promotion_environment_rejects_unattested_roster_member():
    environment = {
        "name": "release-promotion",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 12301237, "login": "mikemcdougall"}},
                    {"type": "User", "reviewer": {"id": 99, "login": "standby-owner"}},
                ],
            },
        ],
    }

    ok, why = cb.validate_environment_metadata(
        environment,
        expected_name="release-promotion",
        expected_reviewer_ids=[12301237],
    )

    assert not ok
    assert "do not match expected roster" in why


def test_missing_binding_is_refused(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "candidate")
    ok, why = _verify({"dry_run": False, "overallStatus": "pass"}, manifest_path, matrix_path)
    assert not ok
    assert "no candidate binding" in why


def test_create_bundle_copies_exact_candidate_bytes_and_binds_copies(tmp_path: Path):
    manifest_path, matrix_path = _files(tmp_path / "input")
    report_path = tmp_path / "gate-report.json"
    report_path.write_text(json.dumps(_live_report(datetime.now(timezone.utc))), encoding="utf-8")
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
