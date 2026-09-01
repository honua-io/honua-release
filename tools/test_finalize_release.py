"""Tests for the release finalizer (the promote step's brain).

The single most important property: promotion FAILS CLOSED — a candidate that is anything but
all-green can never be finalized into a release. Proven here, plus the manifest finalize and the
notes generation against the real committed files.

Run: python -m pytest tools/test_finalize_release.py    (or: python tools/test_finalize_release.py)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_binding as cb  # noqa: E402
import finalize_release as fr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _report(overall, label="2026.1-rc.3", gates=None):
    return {"platform_label": label, "dry_run": False, "overallStatus": overall,
            "gates": gates or [{"gate": "manifest", "decided": "pass"}],
            "evidence_url": "https://example/run/1"}


# ---- verify: fail closed ---------------------------------------------------------------------------
def test_green_candidate_for_matching_label_is_promotable():
    ok, why = fr.verify_gate_report(_report("pass", "2026.1-rc.3"), "2026.1")
    assert ok, why


def test_failing_candidate_is_refused():
    ok, why = fr.verify_gate_report(
        _report("fail", gates=[{"gate": "build-test", "decided": "fail"}]), "2026.1")
    assert not ok and "build-test" in why


def test_blocked_candidate_is_refused():
    ok, why = fr.verify_gate_report(
        _report("blocked", gates=[{"gate": "cloud-parity", "decided": "blocked"}]), "2026.1")
    assert not ok and "cloud-parity" in why


def test_label_mismatch_is_refused():
    ok, why = fr.verify_gate_report(_report("pass", "2026.2-rc.1"), "2026.1")
    assert not ok and "2026.2" in why


def test_allowed_skip_is_promotable():
    # cloud-parity self-skipped (cloud-creds-unset) is on the allowed-skip list -> still promotable.
    rep = _report("pass", gates=[{"gate": "manifest", "status": "pass"},
                                 {"gate": "cloud-parity", "status": "skipped"}])
    ok, why = fr.verify_gate_report(rep, "2026.1")
    assert ok, why
    assert "cloud-parity" in why


def test_skip_of_non_allowlisted_gate_is_refused():
    rep = _report("pass", gates=[{"gate": "manifest", "status": "pass"},
                                 {"gate": "security", "status": "skipped"}])
    ok, why = fr.verify_gate_report(rep, "2026.1")
    assert not ok and "security" in why


def test_non_dict_report_is_refused():
    ok, _ = fr.verify_gate_report("not a report", "2026.1")
    assert not ok


def test_all_green_dry_run_report_is_refused():
    report = _report("pass")
    report["dry_run"] = True
    ok, why = fr.verify_gate_report(report, "2026.1")
    assert not ok
    assert "dry-run" in why


def test_report_without_certification_mode_is_refused():
    report = _report("pass")
    del report["dry_run"]
    ok, why = fr.verify_gate_report(report, "2026.1")
    assert not ok
    assert "dry_run" in why


# ---- finalize --------------------------------------------------------------------------------------
def test_finalize_sets_released_status_and_base_label():
    m = fr.finalize_manifest({"platformRelease": "2026.1-rc.0", "status": "draft", "components": {}},
                             "2026.1-rc.3", "2026-07-01T00:00:00Z")
    assert m["status"] == "released"
    assert m["platformRelease"] == "2026.1"           # -rc stripped
    assert m["releasedDate"] == "2026-07-01T00:00:00Z"


def test_driver_refuses_substituted_candidate_before_writing_release_files(tmp_path):
    certified = tmp_path / "certified"
    certified.mkdir()
    manifest = certified / cb.PLATFORM_MANIFEST
    matrix = certified / cb.COMPATIBILITY_MATRIX
    manifest.write_text("platformRelease: 2026.1-rc.3\nstatus: candidate\ncomponents: {}\n", encoding="utf-8")
    matrix.write_text("matrixVersion: 1\n", encoding="utf-8")
    identity = {
        "source_repository": "honua-io/honua-release",
        "source_sha": "a" * 40,
        "source_branch": "trunk",
        "workflow_path": ".github/workflows/release-train.yml",
        "train_run_id": "28720697360",
        "train_run_attempt": 1,
        "train_run_url": "https://github.com/honua-io/honua-release/actions/runs/28720697360",
        "certification_mode": "live",
    }
    report = cb.bind_gate_report(
        _report("pass"),
        manifest,
        matrix,
        **identity,
    )
    report_path = certified / "gate-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    # Simulate promotion checking out a newer manifest after certification.
    manifest.write_text("platformRelease: 2026.1-rc.9\nstatus: candidate\ncomponents: {}\n", encoding="utf-8")
    out_manifest = tmp_path / "finalized-manifest.yaml"
    out_notes = tmp_path / "release-notes.md"
    rc = fr.main([
        "--label", "2026.1",
        "--gate-report", str(report_path),
        "--released-at", "2026-07-01T00:00:00Z",
        "--manifest", str(manifest),
        "--matrix", str(matrix),
        "--source-repository", identity["source_repository"],
        "--source-sha", identity["source_sha"],
        "--source-branch", identity["source_branch"],
        "--workflow-path", identity["workflow_path"],
        "--train-run-id", identity["train_run_id"],
        "--train-run-attempt", str(identity["train_run_attempt"]),
        "--train-run-url", identity["train_run_url"],
        "--certification-mode", identity["certification_mode"],
        "--out-manifest", str(out_manifest),
        "--out-notes", str(out_notes),
    ])

    assert rc == 1
    assert not out_manifest.exists()
    assert not out_notes.exists()


# ---- release notes against the REAL committed files ------------------------------------------------
def _real():
    import yaml
    return (yaml.safe_load((REPO_ROOT / "platform-manifest.yaml").read_text(encoding="utf-8")),
            yaml.safe_load((REPO_ROOT / "compatibility-matrix.yaml").read_text(encoding="utf-8")))


def test_release_notes_include_every_component_and_header():
    manifest, matrix = _real()
    notes = fr.render_release_notes(
        manifest, matrix, "2026.1", _report("pass"), "https://example/run/1")
    assert notes.startswith("# Honua 2026.1")
    for name in (manifest.get("components") or {}):
        assert name in notes, f"{name} missing from generated notes"
    assert "Breaking changes & upgrade actions" in notes
    assert "Verification & provenance" in notes
    assert "Every wired release gate passed" in notes
    assert "- manifest: passed" in notes


def test_release_notes_name_allowed_skipped_gate_without_claiming_every_gate_passed():
    manifest, matrix = _real()
    report = _report("pass", gates=[
        {"gate": "manifest", "status": "pass"},
        {"gate": "cloud-parity", "status": "skipped"},
    ])

    notes = fr.render_release_notes(manifest, matrix, "2026.1", report)

    assert "- manifest: passed" in notes
    assert "- cloud-parity: skipped (creds-gated; never executed)" in notes
    assert "Every wired release gate passed" not in notes


def test_release_notes_do_not_claim_all_passed_for_empty_or_red_gate_rows():
    manifest, matrix = _real()
    for gates in (
        [],
        [{"gate": "security", "status": "fail"}],
        [{"gate": "upgrade", "status": "blocked"}],
        [{"gate": "future", "status": "unexpected"}],
        ["malformed-row"],
    ):
        report = {"overallStatus": "pass", "gates": gates}
        notes = fr.render_release_notes(manifest, matrix, "2026.1", report)
        assert "Every wired release gate passed" not in notes


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
